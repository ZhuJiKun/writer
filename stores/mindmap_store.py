"""思维导图的本地持久化：读写 mindmaps.json。

节点用扁平列表 + parent 指针存储（parent 为 None 表示根节点，支持多根），
删除节点时递归级联删除整棵子树。
"""

import os
import re
import uuid
from datetime import datetime

from stores.json_store import lock_for, read_json, synchronized, write_json
from stores.paths import CONFIG_DIR

MINDMAP_PATH = os.path.join(CONFIG_DIR, "mindmaps.json")
_LOCK = lock_for(MINDMAP_PATH)

# 全局默认节点样式：白底、蓝框、黑字
DEFAULT_NODE_STYLE = {"bg": "#ffffff", "border": "#4f6ef7", "color": "#26292f"}
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _new_map_id(store):
    ids = {m.get("id") for m in store["maps"]}
    while True:
        mid = "mm_" + uuid.uuid4().hex[:6]
        if mid not in ids:
            return mid


def _new_node_id(m):
    ids = {n.get("id") for n in m["nodes"]}
    while True:
        nid = "n_" + uuid.uuid4().hex[:8]
        if nid not in ids:
            return nid


def load_store():
    base = {"maps": []}
    data = read_json(MINDMAP_PATH)
    if isinstance(data, dict):
        base["maps"] = data.get("maps") or []
    return base


def save_store(store):
    write_json(MINDMAP_PATH, store)


def list_maps():
    """按创建时间倒序（新的在前）。"""
    return sorted(load_store()["maps"], key=lambda m: m.get("created_at", ""), reverse=True)


def get_map(mid):
    for m in load_store()["maps"]:
        if m.get("id") == mid:
            return m
    return None


@synchronized(_LOCK)
def add_map(name, remark=""):
    store = load_store()
    m = {
        "id": _new_map_id(store),
        "name": name.strip(),
        "remark": remark.strip(),
        "created_at": _now(),
        "updated_at": _now(),
        "nodes": [],
    }
    m["nodes"].append({"id": _new_node_id(m), "parent": None, "text": "中心主题",
                       "style": dict(DEFAULT_NODE_STYLE)})
    store["maps"].append(m)
    save_store(store)
    return m


@synchronized(_LOCK)
def update_map_meta(mid, name, remark):
    store = load_store()
    for m in store["maps"]:
        if m.get("id") == mid:
            m["name"] = name.strip() or m["name"]
            m["remark"] = remark.strip()
            m["updated_at"] = _now()
            save_store(store)
            return m
    return None


@synchronized(_LOCK)
def delete_map(mid):
    store = load_store()
    before = len(store["maps"])
    store["maps"] = [m for m in store["maps"] if m.get("id") != mid]
    if len(store["maps"]) != before:
        save_store(store)
        return True
    return False


def _touch(m):
    m["updated_at"] = _now()


@synchronized(_LOCK)
def add_node(mid, parent_id, text):
    """parent_id 为 None/空串时挂为新根节点（用全局默认样式）；
    否则自动复用父节点的样式。父节点不存在返回 None。"""
    store = load_store()
    for m in store["maps"]:
        if m.get("id") != mid:
            continue
        parent_id = parent_id or None
        parent = None
        if parent_id:
            parent = next((n for n in m["nodes"] if n.get("id") == parent_id), None)
            if parent is None:
                return None
        style = dict(parent.get("style")) if parent and parent.get("style") else dict(DEFAULT_NODE_STYLE)
        node = {"id": _new_node_id(m), "parent": parent_id, "text": text.strip() or "新节点",
                "style": style}
        m["nodes"].append(node)
        _touch(m)
        save_store(store)
        return node
    return None


def _valid_style(style, base):
    """只接受 #rrggbb 颜色值，非法项回落到 base 中的值。"""
    out = dict(base)
    for k in ("bg", "border", "color"):
        v = str((style or {}).get(k, "")).strip()
        if _HEX_RE.match(v):
            out[k] = v.lower()
    return out


@synchronized(_LOCK)
def update_node_style(mid, nid, style):
    store = load_store()
    for m in store["maps"]:
        if m.get("id") != mid:
            continue
        for n in m["nodes"]:
            if n.get("id") == nid:
                n["style"] = _valid_style(style, n.get("style") or DEFAULT_NODE_STYLE)
                _touch(m)
                save_store(store)
                return n
        return None
    return None


@synchronized(_LOCK)
def update_node(mid, nid, text):
    store = load_store()
    for m in store["maps"]:
        if m.get("id") != mid:
            continue
        for n in m["nodes"]:
            if n.get("id") == nid:
                n["text"] = text.strip() or n["text"]
                _touch(m)
                save_store(store)
                return n
        return None
    return None


@synchronized(_LOCK)
def delete_node(mid, nid):
    """删除节点及其全部子孙。返回被删节点数，未找到返回 0。"""
    store = load_store()
    for m in store["maps"]:
        if m.get("id") != mid:
            continue
        doomed = {nid}
        grew = True
        while grew:
            grew = False
            for n in m["nodes"]:
                if n.get("parent") in doomed and n.get("id") not in doomed:
                    doomed.add(n["id"])
                    grew = True
        if not any(n.get("id") == nid for n in m["nodes"]):
            return 0
        m["nodes"] = [n for n in m["nodes"] if n.get("id") not in doomed]
        _touch(m)
        save_store(store)
        return len(doomed)
    return 0
