"""章节大纲的本地持久化：读写 outline.json（卷 → 章节 的层级结构）。

章节内容由写作工作台生成后回写，本模块只维护结构数据：
卷（增删改）与章节条目（列表展示、删除、状态）。
"""

import os
import uuid
from datetime import datetime

from json_store import lock_for, read_json, synchronized, write_json

OUTLINE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outline.json")
_LOCK = lock_for(OUTLINE_PATH)

# 章节状态：写作过程状态（写作中/待审校等）归写作工作台管，这里只有这两种
STATUS_TODO = "待生成"
STATUS_DONE = "已生成"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def load_store():
    """返回 {"main": str, "volumes": [...]}；文件缺失/损坏时返回空结构。"""
    base = {"main": "", "volumes": []}
    data = read_json(OUTLINE_PATH)
    if isinstance(data, dict):
        base["main"] = data.get("main") or ""
        base["volumes"] = data.get("volumes") or []
    return base


def save_store(store):
    write_json(OUTLINE_PATH, store)


@synchronized(_LOCK)
def set_main(main):
    store = load_store()
    store["main"] = main
    save_store(store)


# ---------- 卷 ----------

@synchronized(_LOCK)
def add_volume(title, summary=""):
    store = load_store()
    vol = {
        "id": "v_" + uuid.uuid4().hex[:6],
        "title": title,
        "summary": summary,
        "chapters": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    store["volumes"].append(vol)
    save_store(store)
    return vol


def get_volume(vid):
    for v in load_store()["volumes"]:
        if v.get("id") == vid:
            return v
    return None


@synchronized(_LOCK)
def update_volume(vid, title, summary):
    store = load_store()
    for v in store["volumes"]:
        if v.get("id") == vid:
            v["title"] = title
            v["summary"] = summary
            v["updated_at"] = _now()
            save_store(store)
            return v
    return None


@synchronized(_LOCK)
def delete_volume(vid):
    store = load_store()
    store["volumes"] = [v for v in store["volumes"] if v.get("id") != vid]
    save_store(store)


# ---------- 章节 ----------

@synchronized(_LOCK)
def add_chapter(vid, title, summary="", status=STATUS_TODO, word_count=0):
    """供写作工作台/批量规划回写章节条目。"""
    store = load_store()
    for v in store["volumes"]:
        if v.get("id") == vid:
            ch = {
                "id": "ch_" + uuid.uuid4().hex[:6],
                "title": title,
                "summary": summary,
                "status": status,
                "word_count": word_count,
                "created_at": _now(),
                "updated_at": _now(),
            }
            v["chapters"].append(ch)
            v["updated_at"] = _now()
            save_store(store)
            return ch
    return None


@synchronized(_LOCK)
def update_chapter(cid, **fields):
    """更新章节条目的 title/summary/status/word_count 等字段。"""
    store = load_store()
    for v in store["volumes"]:
        for ch in v.get("chapters", []):
            if ch.get("id") == cid:
                for k in ("title", "summary", "status", "word_count"):
                    if k in fields:
                        ch[k] = fields[k]
                ch["updated_at"] = _now()
                v["updated_at"] = _now()
                save_store(store)
                return ch
    return None


def iter_chapters(store=None):
    """按 卷顺序 → 卷内顺序 遍历全部章节，产出 (卷, 章节, 全局章号从1起)。"""
    store = store if store is not None else load_store()
    no = 0
    for v in store["volumes"]:
        for ch in v.get("chapters", []):
            no += 1
            yield v, ch, no


def find_chapter(cid):
    """按章节 id 查找，返回 (卷, 章节, 全局章号)；不存在返回 (None, None, 0)。"""
    for v, ch, no in iter_chapters():
        if ch.get("id") == cid:
            return v, ch, no
    return None, None, 0


@synchronized(_LOCK)
def delete_chapter(cid):
    """删除章节条目，返回被删的章节（不存在返回 None）。"""
    store = load_store()
    for v in store["volumes"]:
        chapters = v.get("chapters", [])
        for i, ch in enumerate(chapters):
            if ch.get("id") == cid:
                removed = chapters.pop(i)
                v["updated_at"] = _now()
                save_store(store)
                return removed
    return None
