"""作品档案的本地持久化：读写 project.json（新书向导的产出物）。"""

import os
from datetime import datetime

from json_store import lock_for, read_json, synchronized, write_json

PROJECT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "project.json")
_LOCK = lock_for(PROJECT_PATH)


def default_project():
    return {
        "title": "",
        "genre": "",
        "audience": "",
        "length": "",
        "logline": "",
        "synopsis": "",
        "background": {"era": "", "rules": "", "stage": ""},
        "style_prompt": "",
        "created_at": "",
        "wizard": {"step": "profile", "history": [], "done": False},
    }


def load_project():
    base = default_project()
    data = read_json(PROJECT_PATH)
    if not isinstance(data, dict):
        return base
    for k, v in data.items():
        if k == "background":
            base["background"].update(v or {})
        elif k == "wizard":
            base["wizard"].update(v or {})
        elif k in base:
            base[k] = v
    return base


@synchronized(_LOCK)
def save_project(proj):
    write_json(PROJECT_PATH, proj)


@synchronized(_LOCK)
def ensure_wizard_opener(openers):
    """当前步骤的向导历史为空时补一条开场白；判空-追加-落盘在锁内完成，并发幂等。"""
    proj = load_project()
    step = proj["wizard"]["step"]
    if not proj["wizard"]["history"] and step in openers:
        proj["wizard"]["history"].append({"role": "assistant", "content": openers[step]})
        save_project(proj)
    return proj


def wizard_done(proj=None):
    proj = proj if proj is not None else load_project()
    return bool(proj.get("wizard", {}).get("done"))


@synchronized(_LOCK)
def update_project(mutator):
    """锁内完成 project 的「读-改-写」（向导对话合并写回用）；返回修改后的 project。"""
    proj = load_project()
    mutator(proj)
    save_project(proj)
    return proj


@synchronized(_LOCK)
def save_setup_fields(fields, background):
    """锁内合并「保存建档」表单的字段并标记建档完成；返回更新后的 project。"""
    proj = load_project()
    for k in ("title", "genre", "audience", "length", "logline", "synopsis", "style_prompt"):
        proj[k] = fields.get(k, "")
    for k in ("era", "rules", "stage"):
        proj["background"][k] = background.get(k, "")
    if not proj["created_at"]:
        proj["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    proj["wizard"]["done"] = True
    save_project(proj)
    return proj


def reset_wizard():
    proj = default_project()
    save_project(proj)
    return proj
