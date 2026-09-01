"""新书向导的对话引擎：优先走已配置的 LLM（generation 槽位），失败或未配置时降级为 mock 规则。"""

import json
import urllib.error
import urllib.request

import config_store as cs

# 向导步骤顺序；推进到 "done" 即收集完毕
STEPS = [
    ("profile", "作品定位"),
    ("premise", "核心创意与概要"),
    ("background", "故事背景"),
    ("style", "文风与叙事规范"),
]

STEP_OPENERS = {
    "profile": "你好，我是你的写作搭档。先聊聊作品定位：这本书是什么题材（玄幻 / 都市 / 科幻 / 悬疑…）？想写给什么样的读者？大概计划写多少章？",
    "premise": "接下来聊核心创意：用一两句话说说这个故事讲什么？（主角是谁，要做什么，最大的冲突是什么）",
    "background": "说说故事发生的世界：时代 / 世界类型、核心规则或力量体系、主要舞台（城市 / 势力）？",
    "style": "最后定文风：希望读起来是什么感觉？（如：诙谐幽默、冷峻克制、热血爽文…）也可以说人称与视角偏好。",
}

_JSON_HINT = (
    "只输出 JSON，不要输出任何其他内容："
    '{"reply": "对用户的回应（中文，简短）", "extracted": {...}, "advance": true 或 false}'
)

_STEP_SYS = {
    "profile": (
        "你是小说写作向导，当前阶段：作品定位。从用户回答中抽取 genre（题材）、"
        "audience（目标读者，可留空）、length（预计篇幅，可留空）。"
        "至少拿到题材时 advance=true，否则继续追问一句。" + _JSON_HINT.replace("{...}", '{"genre":"","audience":"","length":""}')
    ),
    "premise": (
        "你是小说写作向导，当前阶段：核心创意与概要。从用户回答中抽取 logline（一句话梗概，可润色），"
        "并由你补全一版 synopsis（扩展概要，约 150 字，含起因-冲突-结局方向）。"
        "logline 可用即 advance=true。" + _JSON_HINT.replace("{...}", '{"logline":"","synopsis":""}')
    ),
    "background": (
        "你是小说写作向导，当前阶段：故事背景。从用户回答中抽取 "
        "era（时代 / 世界类型）、rules（核心规则 / 力量体系）、stage（主要舞台 / 地理势力）。"
        "用户没提到的项可由你合理补全。抽取后 advance=true。"
        + _JSON_HINT.replace("{...}", '{"era":"","rules":"","stage":""}')
    ),
    "style": (
        "你是小说写作向导，当前阶段：文风与叙事规范。根据用户描述的文风偏好，生成完整的 style_prompt"
        "（写给正文生成模型的提示词，含：语调、人称 / POV、时态、段落习惯、对话比例、禁忌，一段话说清）。"
        "生成后 advance=true。" + _JSON_HINT.replace("{...}", '{"style_prompt":""}')
    ),
    "done": (
        "你是小说写作向导，作品档案已建立。用户会继续提出修改意见或闲聊。"
        "若用户要修改档案，把需要更新的字段放入 extracted（只放要改的字段，键可选："
        "title / genre / audience / length / logline / synopsis / era / rules / stage / style_prompt），"
        "值要完整（如改文风就给出完整的新 style_prompt）；若只是闲聊或确认，extracted 为空对象。"
        "始终 advance=false。" + _JSON_HINT.replace("{...}", '{"title":"","genre":"", "...": "..."}')
    ),
}

def llm_available():
    return slot_available("generation")


def slot_available(slot):
    """指定槽位（generation/critic/extraction）是否有完整可用的配置。"""
    cfg = cs.load_config()
    eff = cs.effective_slot(cfg, slot if slot in cs.SLOTS else "generation")
    return bool(eff.get("base_url") and eff.get("api_key") and eff.get("model"))


def wizard_turn(project, user_msg):
    """处理一轮对话，就地更新 project。返回 {reply, step, done}；失败抛出 LLMError。"""
    if not llm_available():
        raise LLMError("未配置生成模型，请先到「模型配置」页填写 generation 槽位")
    wiz = project["wizard"]
    step = wiz["step"]
    history = wiz["history"]
    history.append({"role": "user", "content": user_msg})

    result = _llm_turn(step, history)

    _apply_extraction(project, step, result.get("extracted") or {})

    reply = result.get("reply", "").strip()
    if result.get("advance") and step != "done":
        wiz["step"] = step = _next_step(step)
        if step == "done":
            reply += "\n\n全部信息收集完毕 🎉 请在下方核对配置，点击「保存建档」完成初始化。"
        else:
            reply += "\n\n" + STEP_OPENERS[step]
    history.append({"role": "assistant", "content": reply})

    return {"reply": reply, "step": step, "done": step == "done"}


def _next_step(step):
    ids = [s[0] for s in STEPS]
    try:
        i = ids.index(step)
    except ValueError:
        return ids[0]
    return ids[i + 1] if i + 1 < len(ids) else "done"


def _apply_extraction(project, step, extracted):
    if step == "profile":
        for k in ("genre", "audience", "length"):
            if extracted.get(k):
                project[k] = str(extracted[k]).strip()
    elif step == "premise":
        for k in ("logline", "synopsis"):
            if extracted.get(k):
                project[k] = str(extracted[k]).strip()
    elif step == "background":
        for k in ("era", "rules", "stage"):
            if extracted.get(k):
                project["background"][k] = str(extracted[k]).strip()
    elif step == "style":
        if extracted.get("style_prompt"):
            project["style_prompt"] = str(extracted["style_prompt"]).strip()
    elif step == "done":
        # 建档后的自由修改：可按需更新任意字段
        for k in ("title", "genre", "audience", "length", "logline", "synopsis", "style_prompt"):
            if extracted.get(k):
                project[k] = str(extracted[k]).strip()
        for k in ("era", "rules", "stage"):
            if extracted.get(k):
                project["background"][k] = str(extracted[k]).strip()


# ---------- LLM 路径 ----------


class LLMError(Exception):
    pass


def _call_llm(messages, slot="generation"):
    """调指定槽位的模型，返回 content 字符串；失败抛 LLMError。"""
    cfg = cs.load_config()
    gen = cs.effective_slot(cfg, slot if slot in cs.SLOTS else "generation")
    payload = json.dumps({
        "model": gen["model"],
        "messages": messages,
    }).encode("utf-8")
    req = urllib.request.Request(
        gen["base_url"].rstrip("/") + "/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + gen["api_key"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8"))
            msg = err.get("error", {}).get("message") or str(err)
        except Exception:
            msg = e.reason
        raise LLMError(f"HTTP {e.code}：{msg}")
    except LLMError:
        raise
    except Exception as e:
        raise LLMError(f"请求失败：{e}")


def _llm_turn(step, history):
    """调模型要求返回结构化 JSON；解析失败时追加上下文重试，最多 3 次。失败抛 LLMError。"""
    sys_prompt = _STEP_SYS.get(step, _STEP_SYS["profile"])
    messages = [{"role": "system", "content": sys_prompt}] + history[-10:]
    last_content = ""
    for _ in range(3):
        content = _call_llm(messages)
        result = _parse_json_result(content)
        if result is not None:
            return result
        last_content = content or ""
        messages = messages + [
            {"role": "assistant", "content": last_content},
            {"role": "user", "content": "你的上一次回复不是合法 JSON。请只输出一个 JSON 对象，格式为 {\"reply\": \"...\", \"extracted\": {...}, \"advance\": true/false}，不要输出任何其他内容。"},
        ]
    raise LLMError("模型连续 3 次未返回合法 JSON，最后输出：" + last_content[:200])


# ---------- 通用 LLM 调用（供向导 / 角色等模块复用） ----------


def chat(messages, slot="generation"):
    """调指定槽位模型，返回 content 字符串；失败抛 LLMError。"""
    return _call_llm(messages, slot=slot)


def extract_json(content):
    """从模型输出里容错提取 JSON 对象，失败返回 None。
    用 JSONDecoder.raw_decode 从首个 '{' 起解码（正确处理嵌套），
    避免贪婪正则把 JSON 之后含 '}' 的文本也吞进来。"""
    if not content:
        return None
    decoder = json.JSONDecoder()
    i = content.find("{")
    while i != -1:
        try:
            data, _ = decoder.raw_decode(content[i:])
        except json.JSONDecodeError:
            i = content.find("{", i + 1)
            continue
        return data if isinstance(data, dict) else None
    return None


def as_bool(v):
    """容错解析模型返回的布尔值：模型把 true/false 输出成字符串时 bool("false") 会反转逻辑。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in ("true", "1", "yes", "y", "是")


def chat_json(sys_prompt, messages, retries=3, slot="generation"):
    """调模型要求返回 JSON 对象；解析失败时追加上下文重试。返回 dict，失败抛 LLMError。"""
    msgs = [{"role": "system", "content": sys_prompt}] + messages
    last_content = ""
    for _ in range(retries):
        content = _call_llm(msgs, slot=slot)
        data = extract_json(content)
        if data is not None:
            return data
        last_content = content or ""
        msgs = msgs + [
            {"role": "assistant", "content": last_content},
            {"role": "user", "content": "你的上一次回复不是合法 JSON。请只输出一个 JSON 对象，不要输出任何其他内容。"},
        ]
    raise LLMError("模型连续 %d 次未返回合法 JSON，最后输出：%s" % (retries, last_content[:200]))


def _parse_json_result(content):
    """从模型输出里容错提取向导结果 JSON。"""
    data = extract_json(content)
    if data is None or "reply" not in data:
        return None
    return {
        "reply": str(data.get("reply", "")),
        "extracted": data.get("extracted") if isinstance(data.get("extracted"), dict) else {},
        "advance": as_bool(data.get("advance")),
    }
