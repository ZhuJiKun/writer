"""写作工作台的本地持久化：读写 chapters.json。

存两类数据：
- batches：生成批次（用户概要 → LLM 细纲方案 → 确认后关联新建的大纲章节）
- contents：按大纲章节 id 存放正文、字数、审校记录与生命周期状态

章节生命周期：planned（已规划/排队）→ generating → reviewing → draft（草稿待采纳）
→ adopted（已采纳，同步进记忆/角色/伏笔）；失败为 failed，可重新生成。
"""

import os
import uuid
from datetime import datetime

from json_store import lock_for, read_json, synchronized, write_json

CHAPTERS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chapters.json")
_LOCK = lock_for(CHAPTERS_PATH)

# 章节内容状态
ST_PLANNED = "planned"
ST_GENERATING = "generating"
ST_REVIEWING = "reviewing"
ST_DRAFT = "draft"
ST_FAILED = "failed"
ST_ADOPTED = "adopted"

# 允许重新生成的状态
REGENERABLE = (ST_PLANNED, ST_FAILED, ST_DRAFT)

STATUS_LABELS = {
    ST_PLANNED: "排队中",
    ST_GENERATING: "生成中",
    ST_REVIEWING: "审校中",
    ST_DRAFT: "草稿",
    ST_FAILED: "失败",
    ST_ADOPTED: "已采纳",
}


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def load_store():
    base = {"batches": [], "contents": {}}
    data = read_json(CHAPTERS_PATH)
    if isinstance(data, dict):
        base["batches"] = [b for b in (data.get("batches") or []) if isinstance(b, dict)]
        base["contents"] = {k: v for k, v in (data.get("contents") or {}).items()
                            if isinstance(v, dict)}
    return base


def save_store(store):
    write_json(CHAPTERS_PATH, store)


# ---------- 批次 ----------

@synchronized(_LOCK)
def new_batch(brief, count, min_words, vol_id, new_vol_title, plan):
    """创建「已规划、待确认」批次。plan: [{"title","summary"}, ...]"""
    store = load_store()
    batch = {
        "id": "b_" + uuid.uuid4().hex[:6],
        "brief": brief,
        "count": count,
        "min_words": min_words,
        "vol_id": vol_id,                # None 表示新建卷
        "new_vol_title": new_vol_title,  # vol_id 为 None 时使用
        "plan": plan,
        "chapter_ids": [],               # 确认后回填（与 plan 顺序一致）
        "status": "planned",             # planned / confirmed / done
        "created_at": _now(),
        "updated_at": _now(),
    }
    store["batches"].append(batch)
    save_store(store)
    return batch


def get_batch(bid):
    for b in load_store()["batches"]:
        if b.get("id") == bid:
            return b
    return None


@synchronized(_LOCK)
def update_batch(bid, **fields):
    store = load_store()
    for b in store["batches"]:
        if b.get("id") == bid:
            b.update(fields)
            b["updated_at"] = _now()
            save_store(store)
            return b
    return None


@synchronized(_LOCK)
def delete_batch(bid):
    store = load_store()
    before = len(store["batches"])
    store["batches"] = [b for b in store["batches"] if b.get("id") != bid]
    if len(store["batches"]) != before:
        save_store(store)
        return True
    return False


def planned_batches():
    return [b for b in load_store()["batches"] if b.get("status") == "planned"]


# ---------- 章节内容 ----------

def new_entry(chapter_id, batch_id="", min_words=1500):
    return {
        "chapter_id": chapter_id,
        "batch_id": batch_id,
        "status": ST_PLANNED,
        "content": "",
        "word_count": 0,
        "min_words": min_words,
        "note": "",                      # 重新生成时的附加要求
        "review": {"rounds": 0, "final": None, "history": []},
        "error": "",
        "created_at": _now(),
        "updated_at": _now(),
        "adopted_at": "",
    }


@synchronized(_LOCK)
def upsert_entry(entry):
    store = load_store()
    entry["updated_at"] = _now()
    store["contents"][entry["chapter_id"]] = entry
    save_store(store)
    return entry


def get_entry(chapter_id):
    return load_store()["contents"].get(chapter_id)


def all_entries():
    return load_store()["contents"]


@synchronized(_LOCK)
def set_status(chapter_id, status, **fields):
    store = load_store()
    entry = store["contents"].get(chapter_id)
    if not entry:
        return None
    entry["status"] = status
    entry.update(fields)
    entry["updated_at"] = _now()
    save_store(store)
    return entry


@synchronized(_LOCK)
def reset_for_regen(chapter_id, note=""):
    """把章节重置为排队重生成：清空正文与审校记录，保留批次关联与字数要求。
    note 为空时同时清空上一次的附加要求，避免残留进 prompt。"""
    store = load_store()
    entry = store["contents"].get(chapter_id)
    if not entry:
        return None
    entry.update({
        "status": ST_PLANNED,
        "content": "",
        "word_count": 0,
        "note": note,
        "review": {"rounds": 0, "final": None, "history": []},
        "error": "",
        "updated_at": _now(),
    })
    save_store(store)
    return entry


@synchronized(_LOCK)
def remove_orphans(valid_chapter_ids):
    """清理大纲中已删除章节的内容记录，返回清理数。"""
    store = load_store()
    keep = set(valid_chapter_ids)
    before = len(store["contents"])
    store["contents"] = {k: v for k, v in store["contents"].items() if k in keep}
    removed = before - len(store["contents"])
    if removed:
        save_store(store)
    return removed
