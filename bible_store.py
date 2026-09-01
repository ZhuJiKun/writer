"""世界观设定库的本地持久化：读写 bible.json（自由分类的设定条目）。"""

import os
import uuid
from datetime import datetime

from json_store import lock_for, read_json, synchronized, write_json

BIBLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bible.json")
_LOCK = lock_for(BIBLE_PATH)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def load_store():
    """返回 {"entries": [...]}；文件缺失/损坏时返回空结构。"""
    base = {"entries": []}
    data = read_json(BIBLE_PATH)
    if isinstance(data, dict):
        base["entries"] = data.get("entries") or []
    return base


def save_store(store):
    write_json(BIBLE_PATH, store)


@synchronized(_LOCK)
def add_entry(category, name, content):
    store = load_store()
    entry = {
        "id": "b_" + uuid.uuid4().hex[:6],
        "category": category,
        "name": name,
        "content": content,
        "created_at": _now(),
        "updated_at": _now(),
    }
    store["entries"].append(entry)
    save_store(store)
    return entry


@synchronized(_LOCK)
def update_entry(eid, category, name, content):
    store = load_store()
    for e in store["entries"]:
        if e.get("id") == eid:
            e["category"] = category
            e["name"] = name
            e["content"] = content
            e["updated_at"] = _now()
            save_store(store)
            return e
    return None


@synchronized(_LOCK)
def delete_entry(eid):
    store = load_store()
    before = len(store["entries"])
    store["entries"] = [e for e in store["entries"] if e.get("id") != eid]
    if len(store["entries"]) != before:
        save_store(store)
        return True
    return False


def existing_names():
    """{(category, name)} 集合，供 LLM 生成结果去重。"""
    return {(e.get("category", ""), e.get("name", "")) for e in load_store()["entries"]}


def grouped():
    """按分类分组，保持条目入库顺序：[(category, [entry, ...]), ...]"""
    groups = {}
    for e in load_store()["entries"]:
        groups.setdefault(e.get("category") or "未分类", []).append(e)
    return list(groups.items())
