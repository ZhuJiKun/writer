"""文风控制的本地持久化：读写 style.json（结构化约束 + 标准样章）。"""

import os
import uuid
from datetime import datetime

from json_store import lock_for, read_json, synchronized, write_json

STYLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "style.json")
_LOCK = lock_for(STYLE_PATH)

CONSTRAINT_KEYS = ["person", "pov", "tense", "paragraph", "dialogue_ratio"]


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def load_store():
    base = {"constraints": {k: "" for k in CONSTRAINT_KEYS}, "samples": []}
    data = read_json(STYLE_PATH)
    if isinstance(data, dict):
        for k in CONSTRAINT_KEYS:
            base["constraints"][k] = str((data.get("constraints") or {}).get(k) or "")
        base["samples"] = data.get("samples") or []
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
