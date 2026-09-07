"""文风控制的本地持久化：读写 style.json（结构化约束 + 标准样章 + 去 AI 味行文禁忌）。"""

import os
import uuid
from datetime import datetime

from stores.json_store import lock_for, read_json, synchronized, write_json
from stores.paths import CONFIG_DIR

STYLE_PATH = os.path.join(CONFIG_DIR, "style.json")
_LOCK = lock_for(STYLE_PATH)

CONSTRAINT_KEYS = ["person", "pov", "tense", "paragraph", "dialogue_ratio"]

# 默认的「去 AI 味」行文禁忌：生成/重写正文时注入 prompt。用户清空并保存即关闭该约束。
DEFAULT_AI_RULES = (
    "- 对话不要拆碎：避免「半句对话＋动作神态＋半句对话」的三段式插入"
    "（如“在想，”他收回神识，“这地方，能待。”）；动作描写放在对话之前或之后写成完整句子，或干脆不写。\n"
    "- 少用神态套话：「淡淡一笑」「嘴角勾起」「眼中闪过一丝……」「收回神识」「不置可否」"
    "这类高频表达能不写就不写，一章内同类表达至多出现一次。\n"
    "- 人物说话要像真人：符合其身份、性格与当下情绪，该说完的话一句说完；"
    "不要用刻意的超短句装深沉（如“能待。”“有意思。”），除非符合人设。\n"
    "- 不用总结式旁白收尾：避免「他知道，从今天起……」「命运的齿轮开始转动」这类跳出剧情的感慨。\n"
    "- 少堆砌：不滥用排比、比喻和形容词；一个意象说清楚就过，不反复渲染。\n"
    "- 心理活动借动作、对话自然呈现，避免大段独白式抒情。"
)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def load_store():
    base = {"constraints": {k: "" for k in CONSTRAINT_KEYS}, "samples": [],
            "ai_rules": DEFAULT_AI_RULES}
    data = read_json(STYLE_PATH)
    if isinstance(data, dict):
        for k in CONSTRAINT_KEYS:
            base["constraints"][k] = str((data.get("constraints") or {}).get(k) or "")
        base["samples"] = data.get("samples") or []
        # 用户清空保存后是空串（关闭约束），不要回退默认值
        if "ai_rules" in data:
            base["ai_rules"] = str(data.get("ai_rules") or "")
    return base


def save_store(store):
    write_json(STYLE_PATH, store)


@synchronized(_LOCK)
def save_constraints(constraints):
    store = load_store()
    for k in CONSTRAINT_KEYS:
        store["constraints"][k] = str(constraints.get(k) or "").strip()
    save_store(store)
    return store["constraints"]


@synchronized(_LOCK)
def save_ai_rules(text):
    """保存「去 AI 味」行文禁忌；空串表示关闭该约束。"""
    store = load_store()
    store["ai_rules"] = str(text or "").strip()
    save_store(store)
    return store["ai_rules"]


@synchronized(_LOCK)
def add_sample(content, source="manual"):
    store = load_store()
    sample = {
        "id": "s_" + uuid.uuid4().hex[:6],
        "content": content,
        "source": source,  # llm / manual
        "created_at": _now(),
    }
    store["samples"].append(sample)
    save_store(store)
    return sample


@synchronized(_LOCK)
def delete_sample(sid):
    store = load_store()
    before = len(store["samples"])
    store["samples"] = [s for s in store["samples"] if s.get("id") != sid]
    if len(store["samples"]) != before:
        save_store(store)
        return True
    return False
