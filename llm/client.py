"""新书向导的对话引擎 + LLM HTTP 客户端（OpenAI / Anthropic 协议）。

向导提示词与开场白统一在 llm/prompts.py；本文件保留步骤推进与抽取应用逻辑。
"""

import json
import urllib.error
import urllib.request

from stores import config_store as cs
from llm import prompts

# 向导步骤顺序；推进到 "done" 即收集完毕
STEPS = [
    ("profile", "作品定位"),
    ("premise", "核心创意与概要"),
    ("background", "故事背景"),
    ("style", "文风与叙事规范"),
]

STEP_OPENERS = prompts.WIZARD_STEP_OPENERS

def llm_available():
    return slot_available("generation")


def slot_available(slot):
    """指定槽位（generation/critic/extraction）是否有完整可用的配置。"""
    cfg = cs.load_config()
    eff = cs.effective_slot(cfg, slot if slot in cs.SLOTS else "generation")
    return bool(eff.get("base_url") and eff.get("api_key") and eff.get("model"))


def wizard_turn(project, user_msg):
    """只读 project 快照做一轮向导对话的 LLM 计算（不修改 project）。

    返回 {"reply", "extracted", "advance", "step"}（step 是本轮发起时所处的向导步骤）；
    消息追加 / 抽取应用 / 步骤推进由 apply_wizard_turn 在锁内完成。失败抛出 LLMError。
    """
    if not llm_available():
        raise LLMError("未配置生成模型，请先到「模型配置」页填写 generation 槽位")
    wiz = project["wizard"]
    step = wiz["step"]
    history = wiz["history"] + [{"role": "user", "content": user_msg}]
    result = _llm_turn(step, history)
    return {"reply": result.get("reply", "").strip(),
            "extracted": result.get("extracted") or {},
            "advance": as_bool(result.get("advance")),
            "step": step}


def apply_wizard_turn(project, user_msg, result):
    """把 wizard_turn 的结果合并进 project（须在 project_store 锁内调用），返回最终回复。

    追加对话消息、按发起时的步骤应用抽取；仅当当前步骤未被并发请求推进过时才推进
    步骤，防止两轮并发对话连跳两步。
    """
    wiz = project["wizard"]
    wiz["history"].append({"role": "user", "content": user_msg})
    _apply_extraction(project, result["step"], result.get("extracted") or {})
    reply = result["reply"]
    step = wiz["step"]
    if result.get("advance") and step == result["step"] and step != "done":
        step = wiz["step"] = _next_step(step)
        if step == "done":
            reply += "\n\n全部信息收集完毕 🎉 请在下方核对配置，点击「保存建档」完成初始化。"
        else:
            reply += "\n\n" + STEP_OPENERS[step]
    wiz["history"].append({"role": "assistant", "content": reply})
    return reply


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


def _anthropic_url(base_url):
    """Anthropic base_url 约定：填到 /v1 则补 /messages，否则补 /v1/messages。"""
    base = base_url.rstrip("/")
    return base + "/messages" if base.endswith("/v1") else base + "/v1/messages"


def _parse_sse_text(raw):
    """解析 Anthropic SSE 流，只拼接 text_delta（跳过 thinking_delta / signature_delta 等）。

    某些转发网关会无视 stream=false 强制返回事件流。流内 error 事件抛 LLMError。
    """
    texts = []
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            evt = json.loads(data)
        except json.JSONDecodeError:
            continue
        etype = evt.get("type")
        if etype == "content_block_delta":
            delta = evt.get("delta") or {}
            if delta.get("type") == "text_delta":
                texts.append(delta.get("text", ""))
        elif etype == "error":
            err = evt.get("error") or {}
            raise LLMError("流式响应错误：%s" % (err.get("message") or evt))
    if not texts:
        raise LLMError("流式响应中未解析到正文内容（可能 max_tokens 被 thinking 消耗殆尽）")
    return "".join(texts)


def _call_anthropic(messages, gen):
    """Anthropic Messages API：system 抽为顶层字段，max_tokens 必填；返回 content 字符串。
    兼容强制 SSE 流式响应的转发网关（如内部 proxy/forward）。"""
    system_parts = [str(m.get("content", "")) for m in messages if m.get("role") == "system"]
    turns = [{"role": m["role"], "content": str(m.get("content", ""))}
             for m in messages if m.get("role") in ("user", "assistant")]
    payload = {
        "model": gen["model"],
        "max_tokens": 8192,
        "messages": turns,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    req = urllib.request.Request(
        _anthropic_url(gen["base_url"]),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": gen["api_key"],
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read().decode("utf-8")
            is_sse = "text/event-stream" in (resp.headers.get("Content-Type") or "")
        if is_sse or raw.lstrip().startswith("event:"):
            return _parse_sse_text(raw)
        body = json.loads(raw)
        return "".join(b.get("text", "") for b in body.get("content", [])
                       if isinstance(b, dict) and b.get("type") == "text")
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


def _call_llm(messages, slot="generation"):
    """调指定槽位的模型，返回 content 字符串；失败抛 LLMError。"""
    cfg = cs.load_config()
    gen = cs.effective_slot(cfg, slot if slot in cs.SLOTS else "generation")
    if gen.get("protocol") == "anthropic":
        return _call_anthropic(messages, gen)
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
    sys_prompt = prompts.WIZARD_STEP_SYS.get(step, prompts.WIZARD_STEP_SYS["profile"])
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


def chat_stream(messages, slot="chat"):
    """流式调指定槽位模型：生成器逐段 yield 文本增量；失败抛 LLMError。

    某些转发网关会无视 stream=true 返回完整 JSON，此时一次性 yield 全部正文。
    """
    cfg = cs.load_config()
    gen = cs.effective_slot(cfg, slot if slot in cs.SLOTS else "generation")
    if gen.get("protocol") == "anthropic":
        yield from _stream_anthropic(messages, gen)
    else:
        yield from _stream_openai(messages, gen)


def _open_stream(req):
    """打开流式请求，返回响应对象；HTTP/网络错误统一抛 LLMError。"""
    try:
        return urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8"))
            msg = err.get("error", {}).get("message") or str(err)
        except Exception:
            msg = e.reason
        raise LLMError(f"HTTP {e.code}：{msg}")
    except Exception as e:
        raise LLMError(f"请求失败：{e}")


def _iter_sse_data(resp):
    """逐行迭代 SSE 流的 data 载荷（跳过空行与 [DONE]）。"""
    for raw_line in resp:
        line = raw_line.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data and data != "[DONE]":
            yield data


def _stream_openai(messages, gen):
    payload = json.dumps({
        "model": gen["model"],
        "messages": messages,
        "stream": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        gen["base_url"].rstrip("/") + "/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + gen["api_key"]},
        method="POST",
    )
    with _open_stream(req) as resp:
        # 网关无视 stream=true 时兜底：按普通 JSON 一次性返回
        if "text/event-stream" not in (resp.headers.get("Content-Type") or ""):
            body = json.loads(resp.read().decode("utf-8"))
            yield body["choices"][0]["message"]["content"]
            return
        for data in _iter_sse_data(resp):
            try:
                evt = json.loads(data)
            except json.JSONDecodeError:
                continue
            for choice in evt.get("choices", []):
                delta = (choice.get("delta") or {}).get("content")
                if delta:
                    yield delta


def _stream_anthropic(messages, gen):
    system_parts = [str(m.get("content", "")) for m in messages if m.get("role") == "system"]
    turns = [{"role": m["role"], "content": str(m.get("content", ""))}
             for m in messages if m.get("role") in ("user", "assistant")]
    payload = {
        "model": gen["model"],
        "max_tokens": 8192,
        "messages": turns,
        "stream": True,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    req = urllib.request.Request(
        _anthropic_url(gen["base_url"]),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": gen["api_key"],
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with _open_stream(req) as resp:
        if "text/event-stream" not in (resp.headers.get("Content-Type") or ""):
            body = json.loads(resp.read().decode("utf-8"))
            yield "".join(b.get("text", "") for b in body.get("content", [])
                          if isinstance(b, dict) and b.get("type") == "text")
            return
        for data in _iter_sse_data(resp):
            try:
                evt = json.loads(data)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type")
            if etype == "content_block_delta":
                delta = evt.get("delta") or {}
                if delta.get("type") == "text_delta":
                    yield delta.get("text", "")
            elif etype == "error":
                err = evt.get("error") or {}
                raise LLMError("流式响应错误：%s" % (err.get("message") or evt))


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
