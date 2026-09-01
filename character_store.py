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
BCK_MIN_INTERVAL = 120  # 秒：备份限流，避免采纳流水线/对话草稿等高频保存产生大量副本
_last_backup_at = 0.0
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
    """写入前把现有 characters.json 备份到 characters_bck/，并清理超过 30 天的旧备份。

    限流：距上次成功备份不足 BCK_MIN_INTERVAL 秒时跳过（高频保存场景下副本会爆炸）；
    进程重启后第一次保存必定备份。
    """
    global _last_backup_at
    if not os.path.exists(CHARS_PATH):
        return
    now = time.time()
    if now - _last_backup_at < BCK_MIN_INTERVAL:
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
    _last_backup_at = now
    cutoff = now - BCK_KEEP_DAYS * 86400
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
def apply_state_updates(cid, updates, note):
    """锁内完成角色动态状态的「读-改-写」：只更新已存在、非空且值不同的键。

    供采纳流水线回写使用——快照的读和写都在锁内，不会与手动编辑互相覆盖。
    返回实际更新的条目数（0 表示无变化，不落盘、不记历史）。
    """
    store = load_store()
    for c in store["characters"]:
        if c.get("id") != cid:
            continue
        n = 0
        for k, v in (updates or {}).items():
            if k in c.get("state", {}) and v and c["state"].get(k) != v:
                c["state"][k] = v
                n += 1
        if n:
            touch_state(c, note)
            c["updated_at"] = _now()
            save_store(store)
        return n
    return 0


@synchronized(_LOCK)
def update_character(cid, mutator):
    """锁内完成角色档案的「读-改-写」（手动编辑 / 对话调整 / 主角覆盖用）。

    mutator 接收锁内读到的角色记录并原地修改；抛 ValueError 视为校验失败，
    放弃落盘并把异常抛给调用方。返回更新后的角色；角色不存在返回 None。
    """
    store = load_store()
    for c in store["characters"]:
        if c.get("id") != cid:
            continue
        mutator(c)
        c["updated_at"] = _now()
        save_store(store)
        return c
    return None


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
    """读草稿快照（仅作 LLM 上下文；写回走 update_draft / promote_draft）。"""
    return load_store()["drafts"].get(sid)


def new_draft():
    return {
        "id": "d_" + uuid.uuid4().hex[:6],
        "history": [],
        "data": {},
        "done": False,
        "created_at": _now(),
    }


@synchronized(_LOCK)
def create_draft():
    """锁内创建并落盘一个新对话草稿，返回草稿。"""
    store = load_store()
    draft = new_draft()
    store["drafts"][draft["id"]] = draft
    save_store(store)
    return draft


@synchronized(_LOCK)
def update_draft(sid, mutator):
    """锁内完成对话草稿的「读-改-写」（对话创建流程的合并写回用）。

    mutator 原地修改草稿；返回更新后的草稿，草稿不存在返回 None。
    """
    store = load_store()
    draft = store["drafts"].get(sid)
    if draft is None:
        return None
    mutator(draft)
    save_store(store)
    return draft


@synchronized(_LOCK)
def promote_draft(sid, char):
    """锁内原子完成「草稿 → 正式角色」：重检姓名唯一、建档、清草稿。

    姓名已被占用时不做任何修改，返回 False；成功返回 True。
    （name_taken 的锁外检查只能用于提前打回，并发下必须以这里的锁内重检为准。）
    """
    store = load_store()
    name = (char.get("name") or "").strip()
    if name and any(c.get("name") == name for c in store["characters"]):
        return False
    char["updated_at"] = _now()
    store["characters"].append(char)
    store["drafts"].pop(sid, None)
    save_store(store)
    return True


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
