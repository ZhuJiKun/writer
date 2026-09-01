"""角色数据的本地持久化：读写 characters.json（角色状态模块的产出物）。"""

import os
import shutil
import time
import uuid
from datetime import datetime

from json_store import lock_for, read_json, synchronized, write_json

CHARS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "characters.json")
BCK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "characters_bck")
BCK_KEEP_DAYS = 30
_LOCK = lock_for(CHARS_PATH)

# 动态状态的固定键（保持与写作时注入 prompt 的结构一致）
STATE_KEYS = ["位置", "身体状态", "心理状态", "当前目标", "已知秘密", "持有物/能力"]


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def new_character(name="", role_type="配角", gender=""):
    return {
        "id": "c_" + uuid.uuid4().hex[:6],
        "name": name,
        "role_type": role_type,
        "gender": gender,
        "personality": "",
        "background": "",
        "appearance": "",
        "state": {k: "" for k in STATE_KEYS},
        "state_history": [],
        "relationships": [],
        "drift_alert": None,
        "source": "manual",
        "chat_history": [],
        "created_at": _now(),
        "updated_at": _now(),
    }


def load_store():
    """返回 {"characters": [...], "drafts": {...}}；文件缺失/损坏时返回空结构。"""
    base = {"characters": [], "drafts": {}}
    data = read_json(CHARS_PATH)
    if isinstance(data, dict):
        base["characters"] = data.get("characters") or []
        base["drafts"] = data.get("drafts") or {}
    return base


def save_store(store):
    _backup_current()
    write_json(CHARS_PATH, store)


def _backup_current():
    """写入前把现有 characters.json 备份到 characters_bck/，并清理超过 30 天的旧备份。"""
    if not os.path.exists(CHARS_PATH):
        return
    os.makedirs(BCK_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BCK_DIR, f"characters_{stamp}.json")
    n = 1
    while os.path.exists(dst):
        n += 1
        dst = os.path.join(BCK_DIR, f"characters_{stamp}_{n}.json")
    try:
        shutil.copy2(CHARS_PATH, dst)
    except OSError:
        return
    cutoff = time.time() - BCK_KEEP_DAYS * 86400
    for name in os.listdir(BCK_DIR):
        p = os.path.join(BCK_DIR, name)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


def load_chars():
    return load_store()["characters"]


def get(cid):
    for c in load_chars():
        if c.get("id") == cid:
            return c
    return None


def has_protagonist():
    return any(c.get("role_type") == "主角" for c in load_chars())


def name_taken(name, exclude_id=None):
    """姓名是否已被其他角色占用（角色名需唯一，采纳回写按名匹配）。"""
    name = (name or "").strip()
    if not name:
        return False
    return any(c.get("name") == name and c.get("id") != exclude_id for c in load_chars())


@synchronized(_LOCK)
def upsert(char):
    store = load_store()
    chars = store["characters"]
    for i, c in enumerate(chars):
        if c.get("id") == char["id"]:
            char["updated_at"] = _now()
            chars[i] = char
            save_store(store)
            return char
    chars.append(char)
    save_store(store)
    return char


@synchronized(_LOCK)
def delete(cid):
    """删除角色，并清理其他角色指向它的关系边。"""
    store = load_store()
    store["characters"] = [c for c in store["characters"] if c.get("id") != cid]
    for c in store["characters"]:
        c["relationships"] = [r for r in c.get("relationships", []) if r.get("target") != cid]
    save_store(store)


def touch_state(char, note):
    """记录一次状态变更。"""
    char.setdefault("state_history", []).append({"at": _now(), "note": note})


# ---------- 新增配角的对话草稿 ----------

def get_draft(sid):
    return load_store()["drafts"].get(sid)


@synchronized(_LOCK)
def save_draft(sid, draft):
    store = load_store()
    store["drafts"][sid] = draft
    save_store(store)


@synchronized(_LOCK)
def pop_draft(sid):
    store = load_store()
    draft = store["drafts"].pop(sid, None)
    save_store(store)
    return draft


def new_draft():
    return {
        "id": "d_" + uuid.uuid4().hex[:6],
        "history": [],
        "data": {},
        "done": False,
        "created_at": _now(),
    }


def relationship_overview(chars):
    """聚合全部关系边：[{from_name, to_name, relation, note}]，含反向展示由模板处理。"""
    by_id = {c["id"]: c for c in chars}
    edges = []
    for c in chars:
        for r in c.get("relationships", []):
            target = by_id.get(r.get("target"))
            if target:
                edges.append({
                    "from_name": c["name"],
                    "to_name": target["name"],
                    "relation": r.get("relation", ""),
                    "note": r.get("note", ""),
                })
    return edges
