# AGENTS.md — 项目维护指南

面向代码 agent 的交接文档。改代码前请先读本文与目标模块的 docstring。

## 项目形态

- 单体 Flask 应用（服务端渲染 Jinja2 模板），**不是 git 仓库，无版本控制，改动无回滚手段**——改前先读文件确认现状，不要覆盖式重写。
- 运行环境：macOS，Python 3 + Flask，依赖仅 `flask`（`markupsafe` 随其安装）。HTTP 请求用标准库 `urllib`，没有 requests/openai SDK。
- 入口 `app.py`；**端口默认 5001**（5000 被 macOS ControlCenter 占用，curl 5000 返回 403 是系统行为，不是应用故障）。`WRITER_PORT` 环境变量可覆盖。
- 后台 nohup 启动时 Werkzeug reloader 自动关闭（stdin 非终端），**改完代码必须重启进程**：`pkill -f "python3 app.py"; nohup python3 app.py > /tmp/writer_server.log 2>&1 &`。
- 模板在 `templates/`（15 个文件，`base.html` 是布局，14 个页面），样式集中在 `static/style.css`。

## 模块职责

### LLM 层

- `llm_client.py`：统一 LLM 调用封装。三槽位 `generation`（正文）/ `critic`（审校）/ `extraction`（抽取摘要），超时 300s；`extract_json()` 用 `JSONDecoder.raw_decode` 解析模型输出；`as_bool()` 把 `"false"` 字符串正确判假——**解析模型的 JSON 布尔字段必须用 `as_bool`，不要用 `bool(data.get(...))`**。
- `content_llm.py`：正文/大纲/审校/采纳抽取等写作相关 prompt 与调用。
- `char_llm.py`：角色相关（生成主角、对话创建/调整角色）。
- 新书向导对话引擎在 `llm_client.py` 前半部分（STEPS：profile→premise→background→style）。

### 存储层（`*_store.py` + `json_store.py`）

- `json_store.py` 是基础设施：`lock_for(path)` 每文件 RLock、`@synchronized(lock)` 装饰器、`read_json`（损坏文件改名 `.corrupt-<时间戳>` 留底）、`write_json`（tmp + `os.replace` 原子写）。
- **所有 store 的写操作（mutator）必须加 `@synchronized(_LOCK)`**，后台写作线程与请求线程会并发读写同一 JSON。新增 store 时照抄现有 9 个的写法。
- 9 个 store 与数据文件一一对应：project / config / character / outline / chapters / memory / bible / foreshadow / style（见 README 数据文件表）。
- `character_store.py` 有 `characters_bck/` 自动备份（近 1 个月），和 `name_taken()` 查重——**角色姓名必须全局唯一**，创建/编辑/生成入口都已拦截重名，新增入口也要调它。

### 写作流水线

- `write_engine.py`：编排核心，带详细 docstring（上下文组装原则务必先读）。内存任务表 `TASKS`：同时只允许一个 running 任务（`active_task()` 前置检查），已结束任务最多保留 20 条自动淘汰；终态判定是「done 计数满 **且** fail==0」才算成功。
- 审校不通过自动重写，`MAX_REVISE_ROUNDS = 2`（最多 3 版正文）；字数不足的 issue 会写入审校历史。
- 采纳（`_apply_adoption`）回写 4 个 store：逐章摘要、伏笔、角色状态、章节状态；角色按名匹配时剔除重名。

### 路由

- 全部路由在 `app.py`（约 60 个，按页面分块）。新增页面前先看同块已有路由的写法。

## 约定与坑

- **XSS**：模板里展示 LLM/用户多行文本用 `{{ m.content | nl2br }}`（`app.py` 里注册的过滤器）。实现细节：必须先 `escape` 再对**纯字符串**做 `replace("\n","<br>")`——`Markup.replace` 会把替换内容也转义，别改回去。
- **表单布尔**：checkbox 取值用显式比较（`== "1"`），不要 `bool(request.form.get(...))`（`"0"` 也是真值）。
- 用户提示文案里带 `%d` 占位的，确认 `%` 参数齐全。
- 删除卷/章节只清大纲；`chapters.json` 孤儿靠进工作台时 `remove_orphans` 自愈，memory 孤儿靠手动「从大纲同步」——这是有意的时滞设计，不是 bug。
- 页面是服务端渲染 + 少量内联 JS，无前端构建；样式改 `static/style.css`，全站风格走简约浅色卡片。

## 验证方式

无单测套件，按以下冒烟验证：

```bash
python3 -m py_compile app.py write_engine.py content_llm.py llm_client.py char_llm.py json_store.py *_store.py
pkill -f "python3 app.py"; nohup python3 app.py > /tmp/writer_server.log 2>&1 &
sleep 2
for p in / /chapter /review /characters /outline /memory /bible /foreshadow /style /settings/models /setup; do
  curl -s -o /dev/null -w "%{http_code} $p\n" "http://127.0.0.1:5001$p"
done   # 期望全部 200
```

截图看样式（headless Chrome）：

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu \
  --window-size=1280,2400 --screenshot=/tmp/x.png "http://127.0.0.1:5001/<页面>"
```

LLM 相关改动需要用户先在「模型配置」里填好可用的 Key 才能端到端验证；无法验证时明确告知用户，不要标注为已完成。

## 数据安全

- `config.json` 含 API Key 明文，`characters.json` 等有自动备份目录；这些都被 `.gitignore` 排除，**不要**把它们提交进任何版本库或外发。
- 用户会并行手工操作页面和数据文件；磁盘内容与预期不符时以读到的现状为准。
