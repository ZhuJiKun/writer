# AGENTS.md — 项目维护指南

面向代码 agent 的交接文档。改代码前请先读本文与目标模块的 docstring。

## 项目形态

- 单体 Flask 应用（服务端渲染 Jinja2 模板）。已是 git 仓库（GitHub: ZhuJiKun/writer，main 分支）；用户数据 JSON 被 .gitignore 排除，**绝不提交**。
- 运行环境：macOS，Python 3 + Flask，依赖仅 `flask`（`markupsafe` 随其安装）。HTTP 请求用标准库 `urllib`，没有 requests/openai SDK。
- 入口 `app.py`；**端口默认 5001**（5000 被 macOS ControlCenter 占用，curl 5000 返回 403 是系统行为，不是应用故障）。`WRITER_PORT` 环境变量可覆盖。
- `app.run(debug=False)`（用户明确要求），**无自动重载，改完代码必须重启进程**：`pkill -f "python3 app.py"; nohup python3 app.py > /tmp/writer_server.log 2>&1 &`。
- 模板在 `templates/`（17 个文件，`base.html` 是布局），样式集中在 `static/style.css`。
- 代码分三层：根目录 `app.py`（路由）+ `write_engine.py`（写作编排）；`stores/` 持久化包；`llm/` LLM 包（提示词集中在 `llm/prompts.py`）。用户数据全部在 `config/`（JSON + `characters_bck/`），路径常量唯一定义在 `stores/paths.py`。

## 模块职责

### LLM 层（`llm/` 包）

- `llm/client.py`：统一 LLM 调用封装。三槽位 `generation`（正文）/ `critic`（审校）/ `extraction`（抽取摘要），超时 300s；支持两种 API 协议——槽位配置里的 `protocol` 字段为 `openai`（`/chat/completions`，Bearer 头）或 `anthropic`（`/v1/messages`，`x-api-key` + `anthropic-version` 头，system 抽为顶层字段），由 `_call_llm` 分发；Anthropic 路径兼容强制 SSE 流式响应的转发网关（`_parse_sse_text` 只拼 `text_delta`，跳过 `thinking_delta`；部分网关未实现 `/v1/models`，拉取模型列表会提示手填）；`extract_json()` 用 `JSONDecoder.raw_decode` 解析模型输出；`as_bool()` 把 `"false"` 字符串正确判假——**解析模型的 JSON 布尔字段必须用 `as_bool`，不要用 `bool(data.get(...))`**。新书向导对话引擎也在本文件（STEPS：profile→premise→background→style）。
- `llm/content.py`：正文/大纲/审校/采纳抽取等写作相关调用逻辑。
- `llm/character.py`：角色相关（生成主角、对话创建/调整角色）。
- `llm/prompts.py`：**全部系统提示词集中于此**（按域分节：新书向导 / 角色 / 世界观 / 伏笔 / 记忆 / 文风 / 写作）；带参提示词是 `p_*` 函数，固定提示词是全大写常量。改措辞只改这个文件。

### 存储层（`stores/` 包）

- `stores/paths.py`：`ROOT_DIR` / `CONFIG_DIR` 路径常量，**数据目录（`config/`）唯一定义处**；store 的数据文件路径一律 `os.path.join(CONFIG_DIR, "xxx.json")`。
- `stores/json_store.py` 是基础设施：`lock_for(path)` 每文件 RLock、`@synchronized(lock)` 装饰器、`read_json`（损坏文件改名 `.corrupt-<时间戳>` 留底）、`write_json`（tmp + `os.replace` 原子写）。
- **所有 store 的写操作（mutator）必须加 `@synchronized(_LOCK)`**，后台写作线程与请求线程会并发读写同一 JSON。新增 store 时照抄现有 9 个的写法。
- 9 个 store 与数据文件一一对应：project / config / character / outline / chapters / memory / bible / foreshadow / style（数据文件在 `config/` 下，见 README 数据文件表）。
- `stores/character_store.py` 有 `config/characters_bck/` 自动备份（近 1 个月，**限流 120 秒一次**，高频保存不会产生副本爆炸），和 `name_taken()` 查重——**角色姓名必须全局唯一**，创建/编辑/生成入口都已拦截重名，新增入口也要调它。
- 跨函数的「读-改-写」也要收进锁内 mutator（如 `character_store.apply_state_updates`、`character_store.update_character`、`character_store.update_draft`、`memory_store.replace_overlapping`、`project_store.ensure_wizard_opener`、`project_store.update_project`），不要在锁外读快照、改完再 upsert——会与并发请求互相覆盖。例外：LLM 调用必须先在锁外拿快照做上下文，写回再走锁内 mutator（见 `character_chat` / `character_new_chat` / `setup_chat`）。
- **聊天流的统一模式**：LLM 层的对话函数只做纯计算、不就地改数据——`llm.client.wizard_turn` / `llm.character.char_chat_turn` 只读快照并返回 `{reply, extracted, ...}` 增量；路由把「追加消息 + 合并 extracted + 状态推进」交给锁内 mutator（`apply_wizard_turn` 经 `update_project`；草稿经 `update_draft`，建档经 `promote_draft`——锁内重检姓名唯一、建档与清草稿原子完成）。`apply_wizard_turn` 仅在当前步骤未被并发推进时才推进步骤，防止连跳。聊天页面前端必须在请求期间禁用发送按钮（setup.html / character_chat.html 的 `sending` 守卫）。

### 写作流水线

- `write_engine.py`：编排核心，带详细 docstring（上下文组装原则务必先读）。内存任务表 `TASKS`：同时只允许一个 running 任务，**占槽必须走 `reserve_task()`（检查+创建在同一把锁内原子完成）**，之后 `launch_task()` 起线程；占槽后若数据写盘失败要 `abort_task()` 释放槽位。路由侧的正确顺序是「先 reserve 再写盘再 launch」（见 `chapter_confirm`）。已结束任务最多保留 20 条自动淘汰；终态判定是「done 计数满 **且** fail==0」才算成功。
- 审校不通过自动重写，`MAX_REVISE_ROUNDS = 2`（最多 3 版正文）；字数不足的 issue 会写入审校历史。
- 采纳（`_apply_adoption`）回写 4 个 store：逐章摘要、伏笔、角色状态、章节状态；角色按名匹配时剔除重名，状态更新走 `cst.apply_state_updates`（锁内完成）。
- 场景锚点 `scene_end`（{"time","place","present"}）：章节 entry 上的本章结尾快照，草稿期由审校顺带输出（`critic_review` 返回值），采纳时由 `extract_adoption` 重抽覆盖，`reset_for_regen` 清空；生成下一章时 `_memory_block` 附硬约束注入。工作台章节列表的锚点列可手动修正 / LLM 补抽（`extract_scene_end`）。

### 路由

- 全部路由在 `app.py`（约 60 个，按页面分块）。新增页面前先看同块已有路由的写法。

## 约定与坑

- **XSS**：模板里展示 LLM/用户多行文本用 `{{ m.content | nl2br }}`（`app.py` 里注册的过滤器）。实现细节：必须先 `escape` 再对**纯字符串**做 `replace("\n","<br>")`——`Markup.replace` 会把替换内容也转义，别改回去。
- **JS 侧的 DOM 注入**：`tojson` 只保证 `<script>` 块内安全，JS 解析回原始字符后再拼进 `innerHTML` 会恢复 HTML 语义。凡把用户/LLM 可控字符串插进 DOM，一律 `createElement` + `textContent` / `value`，不要用 `innerHTML` 拼接（参见 `character_edit.html` 的 `addRelRow`、`settings_models.html` 的 datalist）。
- **字数统计**：统一用 `llm.content.count_words(text)`（去空白、含标点，对齐 Word/网文平台口径），不要 `len(text)`（换行/空格会虚高，导致 min_words 校验偏松）。
- **表单布尔**：checkbox 取值用显式比较（`== "1"`），不要 `bool(request.form.get(...))`（`"0"` 也是真值）。
- 用户提示文案里带 `%d` 占位的，确认 `%` 参数齐全。
- 删除卷/章节只清大纲；`config/chapters.json` 孤儿靠进工作台时 `remove_orphans` 自愈，memory 孤儿靠手动「从大纲同步」——这是有意的时滞设计，不是 bug。
- 页面是服务端渲染 + 少量内联 JS，无前端构建；样式改 `static/style.css`，全站风格走简约浅色卡片。

## 验证方式

无单测套件，按以下冒烟验证：

```bash
python3 -m py_compile app.py write_engine.py llm/*.py stores/*.py
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

- `config/config.json` 含 API Key 明文，`config/characters.json` 等有自动备份目录（`config/characters_bck/`）；整个 `config/` 被 `.gitignore` 排除，**不要**把它们提交进任何版本库或外发。
- 用户会并行手工操作页面和数据文件；磁盘内容与预期不符时以读到的现状为准。
