"""分层记忆的本地持久化：读写 memory.json。

三层结构：近章原文（写作工作台接入后填充，本文件不存原文）、
逐章摘要（来源=细纲同步或正文抽取）、合并摘要（LLM 跨章压缩）。
"""

import os
import re
import uuid
from datetime import datetime

from json_store import lock_for, read_json, synchronized, write_json

MEMORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.json")
_LOCK = lock_for(MEMORY_PATH)

SOURCE_OUTLINE = "细纲"
SOURCE_TEXT = "正文"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def load_store():
    base = {"chapter_summaries": [], "merged_summaries": []}
    data = read_json(MEMORY_PATH)
    if isinstance(data, dict):
        base["chapter_summaries"] = data.get("chapter_summaries") or []
        base["merged_summaries"] = [_backfill_range(m) for m in (data.get("merged_summaries") or [])
                                    if isinstance(m, dict)]
    return base


_RANGE_RE = re.compile(r"第(\d+)(?:\s*-\s*(\d+))?\s*章")


def _backfill_range(m):
    """旧合并条目缺少结构化范围字段时，从 range 文本（如「第1-10章 · 卷名」）解析回填。"""
    if isinstance(m.get("from_no"), int) and isinstance(m.get("to_no"), int):
        return m
    match = _RANGE_RE.search(m.get("range") or "")
    if match:
        m["from_no"] = int(match.group(1))
        m["to_no"] = int(match.group(2) or match.group(1))
    return m


def save_store(store):
    write_json(MEMORY_PATH, store)


@synchronized(_LOCK)
def upsert_chapter_summary(chapter_id, no, vol, title, summary, source=SOURCE_OUTLINE):
    """按 chapter_id 覆盖式写入逐章摘要（细纲同步与将来正文抽取共用）。"""
    store = load_store()
    for cs in store["chapter_summaries"]:
        if cs.get("chapter_id") == chapter_id:
            cs.update({"no": no, "vol": vol, "title": title, "summary": summary,
                       "source": source, "updated_at": _now()})
            save_store(store)
            return cs
    item = {"chapter_id": chapter_id, "no": no, "vol": vol, "title": title,
            "summary": summary, "source": source, "updated_at": _now()}
    store["chapter_summaries"].append(item)
    save_store(store)
    return item


@synchronized(_LOCK)
def remove_stale(chapter_ids):
    """删除大纲中已不存在的章节摘要，返回删除条数。"""
    store = load_store()
    keep = set(chapter_ids)
    before = len(store["chapter_summaries"])
    store["chapter_summaries"] = [c for c in store["chapter_summaries"] if c.get("chapter_id") in keep]
    removed = before - len(store["chapter_summaries"])
    if removed:
        save_store(store)
    return removed


@synchronized(_LOCK)
def set_merged(ranges):
    """整体替换合并摘要层。ranges: [{"range": str, "summary": str}, ...]"""
    store = load_store()
    store["merged_summaries"] = [
        {"id": "m_" + uuid.uuid4().hex[:6], "range": str(r.get("range", "")).strip(),
         "summary": str(r.get("summary", "")).strip(), "created_at": _now()}
        for r in ranges if r.get("summary")
    ]
    save_store(store)
    return store["merged_summaries"]


@synchronized(_LOCK)
def delete_merged(mid):
    store = load_store()
    before = len(store["merged_summaries"])
    store["merged_summaries"] = [m for m in store["merged_summaries"] if m.get("id") != mid]
    if len(store["merged_summaries"]) != before:
        save_store(store)
        return True
    return False


def covered_nos():
    """已被合并摘要覆盖的章节号集合（仅统计有结构化范围字段的条目）。"""
    nos = set()
    for m in load_store()["merged_summaries"]:
        a, b = m.get("from_no"), m.get("to_no")
        if isinstance(a, int) and isinstance(b, int):
            nos.update(range(a, b + 1))
    return nos


@synchronized(_LOCK)
def append_merged(segments):
    """追加合并摘要段。segments: [{from_no, to_no, range, summary}, ...]"""
    store = load_store()
    for s in segments:
        if not s.get("summary"):
            continue
        store["merged_summaries"].append({
            "id": "m_" + uuid.uuid4().hex[:6],
            "from_no": s.get("from_no"), "to_no": s.get("to_no"),
            "range": str(s.get("range", "")).strip(),
            "summary": str(s.get("summary", "")).strip(),
            "created_at": _now()})
    save_store(store)
    return store["merged_summaries"]


@synchronized(_LOCK)
def delete_overlapping(from_no, to_no):
    """删除与 [from_no, to_no] 区间相交的合并摘要段（仅判断有结构化字段的），返回删除数。"""
    store = load_store()

    def overlaps(m):
        a, b = m.get("from_no"), m.get("to_no")
        return isinstance(a, int) and isinstance(b, int) and a <= to_no and from_no <= b

    before = len(store["merged_summaries"])
    store["merged_summaries"] = [m for m in store["merged_summaries"] if not overlaps(m)]
    removed = before - len(store["merged_summaries"])
    if removed:
        save_store(store)
    return removed
