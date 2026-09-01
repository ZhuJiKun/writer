"""伏笔追踪的本地持久化：读写 foreshadow.json。"""

import os
import re
from datetime import datetime

from json_store import lock_for, read_json, synchronized, write_json

FORESHADOW_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "foreshadow.json")
_LOCK = lock_for(FORESHADOW_PATH)

STATUSES = ["待回收", "已回收", "长线"]


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def load_store():
    base = {"items": []}
    data = read_json(FORESHADOW_PATH)
    if isinstance(data, dict):
        base["items"] = data.get("items") or []
    return base


def save_store(store):
    write_json(FORESHADOW_PATH, store)


def next_id(store=None):
    """F-001 起自增编号。"""
    store = store if store is not None else load_store()
    top = 0
    for it in store["items"]:
        m = re.match(r"F-(\d+)$", str(it.get("id", "")))
        if m:
            top = max(top, int(m.group(1)))
    return "F-%03d" % (top + 1)


@synchronized(_LOCK)
def add_item(content, planted="", plan_recycle="", status="待回收"):
    store = load_store()
    item = {
        "id": next_id(store),
        "content": content,
        "planted": planted,
        "plan_recycle": plan_recycle,
        "status": status if status in STATUSES else "待回收",
        "created_at": _now(),
        "updated_at": _now(),
    }
    store["items"].append(item)
    save_store(store)
    return item


@synchronized(_LOCK)
def update_item(fid, content, planted, plan_recycle, status):
    store = load_store()
    for it in store["items"]:
        if it.get("id") == fid:
            it["content"] = content
            it["planted"] = planted
            it["plan_recycle"] = plan_recycle
            it["status"] = status if status in STATUSES else "待回收"
            it["updated_at"] = _now()
            save_store(store)
            return it
    return None


@synchronized(_LOCK)
def delete_item(fid):
    store = load_store()
    before = len(store["items"])
    store["items"] = [it for it in store["items"] if it.get("id") != fid]
    if len(store["items"]) != before:
        save_store(store)
        return True
    return False


def due_items(current_chapter):
    """已到计划回收点仍未回收的伏笔：plan_recycle 中的章节号 <= current_chapter。"""
    due = []
    for it in load_store()["items"]:
        if it.get("status") != "待回收":
            continue
        nums = [int(n) for n in re.findall(r"\d+", str(it.get("plan_recycle", "")))]
        if nums and min(nums) <= current_chapter:
            due.append(it)
    return due
