"""作品档案的本地持久化：读写 project.json（新书向导的产出物）。"""

import os

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


def wizard_done(proj=None):
    proj = proj if proj is not None else load_project()
    return bool(proj.get("wizard", {}).get("done"))


def reset_wizard():
    proj = default_project()
    save_project(proj)
    return proj
