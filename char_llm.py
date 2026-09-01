"""角色模块的 LLM 能力：从作品档案生成主角、对话式创建/调整角色。"""

import json

import character_store as cst
from llm_client import LLMError, as_bool, chat_json, llm_available

_STATE_KEYS_DESC = "、".join(cst.STATE_KEYS)

_CHAR_JSON_SCHEMA = (
    '{"name":"","gender":"男/女/其他","personality":"性格（2-3 句）",'
    '"background":"背景来历（2-4 句）","appearance":"外貌形象（1-2 句）",'
    '"state":{"位置":"","身体状态":"","心理状态":"","当前目标":"","已知秘密":"","持有物/能力":""},'
    '"relationships":[{"target_name":"其他角色名","relation":"关系（如 师徒/亦敌亦友）","note":"补充说明，可空"}]}'
)


def generate_protagonist(project):
    """根据作品档案一次性生成主角档案 dict；失败抛 LLMError。"""
    if not llm_available():
        raise LLMError("未配置生成模型，请先到「模型配置」页填写 generation 槽位")
    bg = project.get("background", {})
    sys_prompt = (
        "你是小说角色设定师。根据下面的作品档案，为主角生成完整的人物档案。\n"
        "要求：人物要贴合题材与故事概要，性格和背景要有记忆点，外貌具体到可描写；"
        "state 是故事开局时主角的动态状态，各项填开局值。\n"
        "只输出 JSON，不要输出任何其他内容：" + _CHAR_JSON_SCHEMA + "\n\n"
        "【作品档案】\n"
        f"书名：{project.get('title') or '（未定）'}\n"
        f"题材：{project.get('genre')}\n"
        f"一句话梗概：{project.get('logline')}\n"
        f"故事概要：{project.get('synopsis')}\n"
        f"时代/世界：{bg.get('era')}\n"
        f"规则/力量体系：{bg.get('rules')}\n"
        f"舞台/势力：{bg.get('stage')}\n"
    )
    data = chat_json(sys_prompt, [{"role": "user", "content": "请生成主角档案。"}])
    if not data.get("name"):
        raise LLMError("模型返回的角色缺少 name 字段")
    return _normalize_char_data(data)


def char_chat_turn(draft, user_msg, char=None):
    """对话式创建（char=None）或调整（char 为现有角色）角色的一轮 LLM 计算。

    只读 draft 快照（{"history": [...], "data": {...}}），不做任何修改；
    返回 {"reply", "done", "extracted"}：extracted 是本轮抽取的字段增量，
    消息追加与字段合并由调用方在锁内完成（见 merge_extracted_data）。
    done=True 表示档案已齐可入库。失败抛 LLMError。
    """
    if not llm_available():
        raise LLMError("未配置生成模型，请先到「模型配置」页填写 generation 槽位")
    history = draft.get("history", []) + [{"role": "user", "content": user_msg}]
    data = chat_json(_build_chat_sys(char), history[-12:])

    reply = str(data.get("reply", "")).strip()
    extracted = data.get("extracted") if isinstance(data.get("extracted"), dict) else {}
    done = as_bool(data.get("done"))
    if done and not ((draft.get("data") or {}).get("name") or extracted.get("name")):
        done = False
        reply = reply or "还差最关键的一步：这个角色叫什么名字？"
    return {"reply": reply, "done": done, "extracted": extracted}


def merge_extracted_data(data, extracted):
    """把一轮对话抽取的字段增量清洗后合并进 data（纯函数，供锁内 mutator 调用）。"""
    if extracted:
        _merge_extracted(data, extracted)


def _roster(exclude_id=None):
    """现有角色名单，注入提示词让 LLM 用真实名字写关系。"""
    lines = [f"- {c['name']}（{c['role_type']}）" for c in cst.load_chars()
             if c.get("id") != exclude_id and c.get("name")]
    if not lines:
        return ""
    return ("\n【已存在的角色】\n" + "\n".join(lines)
            + "\nrelationships 的 target_name 必须严格使用上面名单里的名字；与名单外角色的关系不要写。\n")


def _build_chat_sys(char):
    base = (
        "你是小说角色设定师，通过与用户对话来" + ("创建" if char is None else "修改")
        + "一个小说角色（" + ("配角" if char is None else char.get("role_type", "角色")) + "）。\n"
        "每轮从用户回答中抽取已知字段放入 extracted；信息不足时继续追问一两个关键问题"
        "（姓名、性别、性格、背景、与主角的关系、故事开局时的状态）；"
        "当姓名、性格、背景都已明确时 done=true，并把剩余空缺字段由你合理补全后一并放入 extracted。\n"
        "reply 用中文、简短自然。" + _JSON_HINT
    )
    if char is None:
        return base + _roster() + "\n抽取字段格式：" + _CHAR_JSON_SCHEMA
    # 调整已有角色：只放要改的字段
    snapshot = json.dumps({
        "name": char.get("name"), "gender": char.get("gender"),
        "role_type": char.get("role_type"), "personality": char.get("personality"),
        "background": char.get("background"), "appearance": char.get("appearance"),
        "state": char.get("state"), "relationships": char.get("relationships"),
    }, ensure_ascii=False)
    return base + _roster(exclude_id=char.get("id")) + (
        "\nextracted 只放用户要求修改的字段（值要完整，如改性格就给完整新性格）；"
        "用户只是闲聊或确认时 extracted 为空对象；done 始终为 false。\n"
        "【当前角色档案】\n" + snapshot + "\n抽取字段格式：" + _CHAR_JSON_SCHEMA
    )


_JSON_HINT = (
    "只输出 JSON，不要输出任何其他内容："
    '{"reply":"对用户的回应","extracted":{...},"done":true或false}'
)


def _merge_extracted(dst, extracted):
    """把 LLM 抽取的字段合并进 draft data。"""
    for k in ("name", "gender", "role_type", "personality", "background", "appearance"):
        v = extracted.get(k)
        if v:
            dst[k] = str(v).strip()
    state = extracted.get("state")
    if isinstance(state, dict):
        dst.setdefault("state", {})
        for k, v in state.items():
            if v:
                dst["state"][str(k)] = str(v).strip()
    rels = extracted.get("relationships")
    if isinstance(rels, list):
        dst["relationships"] = [
            {"target_name": str(r.get("target_name", "")).strip(),
             "relation": str(r.get("relation", "")).strip(),
             "note": str(r.get("note", "")).strip()}
            for r in rels if isinstance(r, dict) and r.get("target_name")
        ]


def _normalize_char_data(data):
    """清洗一次性生成的角色 JSON，保证结构完整。"""
    out = {}
    for k in ("name", "gender", "personality", "background", "appearance"):
        out[k] = str(data.get(k) or "").strip()
    state = data.get("state") if isinstance(data.get("state"), dict) else {}
    out["state"] = {k: str(state.get(k) or "").strip() for k in cst.STATE_KEYS}
    out["relationships"] = []
    return out


def apply_char_data(char, data, chars):
    """把抽取/生成的 data 写进角色记录（关系 target_name 解析为 id）。"""
    by_name = {c["name"]: c["id"] for c in chars}
    for k in ("name", "gender", "role_type", "personality", "background", "appearance"):
        if data.get(k):
            char[k] = str(data[k]).strip()
    for k, v in (data.get("state") or {}).items():
        if k in char["state"] and v:
            char["state"][k] = str(v).strip()
    if data.get("relationships"):
        rels = []
        for r in data["relationships"]:
            tid = by_name.get(r.get("target_name", ""))
            if tid:
                rels.append({"target": tid, "relation": r.get("relation", ""), "note": r.get("note", "")})
        char["relationships"] = rels


def build_character(char_data, role_type, source, chars):
    """把抽取/生成的 data 组装成完整角色记录。"""
    c = cst.new_character(char_data.get("name", ""), role_type, char_data.get("gender", ""))
    c["source"] = source
    apply_char_data(c, char_data, chars)
    return c
