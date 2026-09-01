"""世界观 / 伏笔 / 文风 / 记忆 / 写作工作台五个模块的 LLM 生成能力。

每个函数都是一次 chat_json / chat 调用；未配置模型时抛 LLMError。
槽位：规划与正文用 generation，审校用 critic，采纳抽取用 extraction。
"""

import json
import re

from llm_client import LLMError, as_bool, chat, chat_json, llm_available

_WS_RE = re.compile(r"\s+")


def count_words(text):
    """网文/Word 口径的「字数」：去掉空白字符后的字符数（含标点）。"""
    return len(_WS_RE.sub("", text or ""))


def _require_llm():
    if not llm_available():
        raise LLMError("未配置生成模型，请先到「模型配置」页填写 generation 槽位")


def _project_brief(project):
    bg = project.get("background", {})
    return (
        f"书名：{project.get('title') or '（未定）'}\n"
        f"题材：{project.get('genre')}\n"
        f"一句话梗概：{project.get('logline')}\n"
        f"故事概要：{project.get('synopsis')}\n"
        f"时代/世界：{bg.get('era')}\n"
        f"规则/力量体系：{bg.get('rules')}\n"
        f"舞台/势力：{bg.get('stage')}\n"
    )


# ---------- 世界观设定库 ----------

def init_bible(project):
    """依据作品档案生成初始设定条目。返回 [{"category","name","content"}, ...]。"""
    _require_llm()
    sys_prompt = (
        "你是小说设定师。根据下面的作品档案，为这本书建立世界观设定库（Story Bible）。\n"
        "要求：覆盖时间线、地理/舞台、力量体系、势力组织、专有名词、重要物品等分类，"
        "每个分类 2~4 条，条目要具体、可直接用于写作时检索注入；不要编造与档案冲突的内容。\n"
        "只输出 JSON：{\"entries\":[{\"category\":\"分类名\",\"name\":\"条目名\",\"content\":\"条目内容（1-3 句）\"}]}\n\n"
        "【作品档案】\n" + _project_brief(project)
    )
    data = chat_json(sys_prompt, [{"role": "user", "content": "请生成初始设定库。"}])
    return _clean_entries(data)


def extend_bible_category(project, category, existing_names, hint=""):
    """给指定分类补充 1~3 条设定。返回 [{"category","name","content"}, ...]。"""
    _require_llm()
    names = "、".join(existing_names) or "（空）"
    sys_prompt = (
        f"你是小说设定师。为作品的世界观设定库补充「{category}」分类下的条目，1~3 条。\n"
        f"该分类已有条目：{names}。不要重复已有条目；新条目要具体、与作品档案一致。\n"
        "只输出 JSON：{\"entries\":[{\"category\":\"" + category + "\",\"name\":\"条目名\",\"content\":\"条目内容（1-3 句）\"}]}\n\n"
        "【作品档案】\n" + _project_brief(project)
    )
    user = "请补充条目。" + (f"补充方向：{hint}" if hint else "")
    data = chat_json(sys_prompt, [{"role": "user", "content": user}])
    return _clean_entries(data, fallback_category=category)


def _clean_entries(data, fallback_category=""):
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise LLMError("模型返回缺少 entries 数组")
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        content = str(e.get("content") or "").strip()
        category = str(e.get("category") or fallback_category or "未分类").strip()
        if name and content:
            out.append({"category": category, "name": name, "content": content})
    if not out:
        raise LLMError("模型未返回任何有效设定条目")
    return out


# ---------- 伏笔追踪 ----------

def brainstorm_foreshadows(project, outline):
    """依据主线 + 各卷章细纲头脑风暴伏笔。返回 [{"content","planted","plan_recycle","status"}, ...]。"""
    _require_llm()
    sys_prompt = (
        "你是小说策划。根据作品档案与章节大纲，为这本书设计 3~5 条伏笔。\n"
        "要求：伏笔要具体（谁、什么秘密/物件/事件），planted 写建议埋设位置（如「第3章」），"
        "plan_recycle 写建议回收位置（如「第40章」或「第三卷」），status 从「待回收/长线」中选"
        "（贯穿全书的大伏笔用「长线」）。伏笔应贴合已有大纲，不要凭空改变主线。\n"
        "只输出 JSON：{\"items\":[{\"content\":\"伏笔内容\",\"planted\":\"埋设位置\",\"plan_recycle\":\"计划回收\",\"status\":\"待回收\"}]}\n\n"
        "【作品档案】\n" + _project_brief(project) + "\n【章节大纲】\n" + _outline_brief(outline)
    )
    data = chat_json(sys_prompt, [{"role": "user", "content": "请设计伏笔。"}])
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise LLMError("模型返回缺少 items 数组")
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        content = str(it.get("content") or "").strip()
        if content:
            out.append({
                "content": content,
                "planted": str(it.get("planted") or "").strip(),
                "plan_recycle": str(it.get("plan_recycle") or "").strip(),
                "status": str(it.get("status") or "待回收").strip(),
            })
    if not out:
        raise LLMError("模型未返回任何有效伏笔")
    return out


def _outline_brief(outline):
    lines = [f"全书主线：{outline.get('main') or '（未定）'}"]
    for v in outline.get("volumes", []):
        lines.append(f"\n■ {v.get('title')}：{v.get('summary') or ''}")
        for i, ch in enumerate(v.get("chapters", [])[:30], 1):
            lines.append(f"  {i}. {ch.get('title')}——{ch.get('summary') or ''}")
        if len(v.get("chapters", [])) > 30:
            lines.append(f"  …（共 {len(v['chapters'])} 章）")
    return "\n".join(lines)


# ---------- 分层记忆 ----------

def compress_window(chapters):
    """把一组连续章节的逐章摘要（≤10 章、同卷）压缩成一段合并摘要。返回摘要 str。"""
    _require_llm()
    lines = [f"第{c['no']}章《{c['title']}》：{c['summary']}" for c in chapters]
    sys_prompt = (
        "你是小说内容编辑。把下面这组连续章节的摘要有损压缩成一段「范围摘要」，供长篇写作的远期记忆检索。\n"
        "要求：3-6 句话；保留关键事实（人物关系变化、重要事件、设定变更、伏笔埋收），舍弃细节描写；"
        "可用「第X-Y章」指代章节，不要虚构材料中没有的情节。\n"
        "只输出 JSON：{\"summary\":\"压缩摘要\"}"
    )
    data = chat_json(sys_prompt, [{"role": "user", "content": "\n".join(lines)}])
    summary = str(data.get("summary") or "").strip() if isinstance(data, dict) else ""
    if not summary:
        raise LLMError("模型未返回有效摘要")
    return summary


def extract_chapter_summary(title, text):
    """从章节正文抽取事实摘要（3-5 句）。返回摘要 str。供写作工作台生成正文后调用。"""
    _require_llm()
    sys_prompt = (
        "你是小说内容编辑。阅读下面的章节正文，抽取一段事实摘要，供长篇写作的记忆检索。\n"
        "要求：3-5 句话；只记录正文中实际发生的事实——关键事件、人物状态与关系变化、"
        "新出现的设定或伏笔；不要评价文字好坏，不要虚构。\n"
        "只输出 JSON：{\"summary\":\"事实摘要\"}"
    )
    data = chat_json(sys_prompt, [{"role": "user", "content": f"章节标题：{title}\n\n{text}"}])
    summary = str(data.get("summary") or "").strip() if isinstance(data, dict) else ""
    if not summary:
        raise LLMError("模型未返回有效摘要")
    return summary


# ---------- 文风控制 ----------

def extract_style_constraints(style_prompt):
    """从 style_prompt 解析结构化约束。返回 {person, pov, tense, paragraph, dialogue_ratio}。"""
    _require_llm()
    sys_prompt = (
        "你是写作风格分析师。从下面的文风提示词中提取结构化约束。\n"
        "只输出 JSON：{\"person\":\"人称\",\"pov\":\"视角约束\",\"tense\":\"时态\","
        "\"paragraph\":\"段落习惯\",\"dialogue_ratio\":\"对话比例\"}；某项未提及就填空字符串。"
    )
    data = chat_json(sys_prompt, [{"role": "user", "content": style_prompt}])
    return {k: str(data.get(k) or "").strip()
            for k in ("person", "pov", "tense", "paragraph", "dialogue_ratio")}


def generate_style_sample(project, style_prompt):
    """依据文风规范 + 作品概要生成一段标准样章。返回样章文本 str。"""
    _require_llm()
    sys_prompt = (
        "你是小说作家。严格按照下面的文风规范，为这部作品写一段 100~200 字的样章"
        "（场景自选，要能体现该书最典型的文风，将作为 few-shot 样例注入后续写作）。\n"
        "只输出 JSON：{\"sample\":\"样章正文\"}\n\n"
        f"【文风规范】\n{style_prompt}\n\n【作品档案】\n" + _project_brief(project)
    )
    data = chat_json(sys_prompt, [{"role": "user", "content": "请生成样章。"}])
    sample = str(data.get("sample") or "").strip()
    if not sample:
        raise LLMError("模型返回的 sample 为空")
    return sample


# ---------- 写作工作台（规划 / 生成 / 审校 / 重写 / 采纳抽取） ----------

def _require_slot(slot):
    from llm_client import slot_available
    if not slot_available(slot):
        raise LLMError("未配置%s模型，请先到「模型配置」页检查" %
                       {"generation": "生成", "critic": "审校", "extraction": "抽取"}.get(slot, slot))


def plan_chapters(ctx, brief, count, start_no, vol_label):
    """根据上下文 + 用户概要规划 count 章细纲（generation 槽位）。

    ctx 为 write_engine 组装的上下文文本。返回 [{"title","summary"}, ...]，长度 == count。
    """
    _require_slot("generation")
    sys_prompt = (
        "你是网络小说策划。根据作品上下文和用户对这批章节的内容构想，规划接下来 %d 章的细纲"
        "（从第%d章起，归入「%s」）。\n"
        "要求：承接已有剧情与角色状态，自然呼应待回收伏笔，逐章推进用户构想的情节并有节奏地拆分冲突；"
        "每章 title 2~8 个字、有钩子；summary 3~4 句，写清关键事件、出场人物、状态/关系变化，可直接指导正文写作；"
        "不要与已有章节重复。\n"
        "只输出 JSON：{\"chapters\":[{\"title\":\"\",\"summary\":\"\"}]}，必须正好 %d 章。\n\n"
        "【作品上下文】\n%s"
        % (count, start_no, vol_label, count, ctx)
    )
    msgs = [{"role": "user", "content": "本批内容构想：" + brief}]
    last = None
    for _ in range(2):  # 章数不符时重试一次
        data = chat_json(sys_prompt, msgs)
        items = data.get("chapters") if isinstance(data, dict) else None
        plan = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "").strip()
            summary = str(it.get("summary") or "").strip()
            if title and summary:
                plan.append({"title": title, "summary": summary})
        if len(plan) >= count:
            return plan[:count]
        last = len(plan)
        msgs = msgs + [
            {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
            {"role": "user", "content": "你只给了 %d 章，需要正好 %d 章。请重新输出完整的 %d 章细纲 JSON。"
                                        % (last, count, count)},
        ]
    raise LLMError("模型两次返回的章数都不足（要 %d 章，得 %d 章），请换个说法重试" % (count, last or 0))


def generate_chapter(ctx, title, min_words, note=""):
    """生成一章正文（generation 槽位），返回正文字符串。"""
    _require_slot("generation")
    sys_prompt = (
        "你是网络小说作家，正在创作第%d章《%s》。\n"
        "要求：严格遵守上下文里的文风规范与 POV 约束；正文不少于 %d 字（汉字，不含标点也不宜太少，宁多勿少）；"
        "情节以本章细纲为纲，可合理发挥细节、对话与氛围；段落宜短，对话用「」；"
        "只输出章节正文，不要输出标题、章节号、大纲或任何说明文字。\n\n"
        "【作品上下文】\n%s"
        % (ctx.get("no", 0), title, min_words, ctx["text"])
    )
    user = "请写出本章正文。"
    if note:
        user += " 额外要求：" + note
    text = chat([{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": user}]).strip()
    text = _strip_junk(text, title)
    if not text:
        raise LLMError("模型返回的正文为空")
    return text


def _strip_junk(text, title):
    """去掉模型可能加在正文前的标题行 / markdown 围栏。"""
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines:
        first = lines[0].strip().strip("#").strip()
        if first.startswith("```") or first == title or \
                (len(first) <= 30 and title in first and ("章" in first or "《" in first)):
            lines.pop(0)
    return "\n".join(lines).strip().strip("`").strip()


def critic_review(ctx_brief, no, title, text, min_words):
    """审校一章正文（critic 槽位）。返回 {"pass":bool,"score":int,"issues":[{"type","detail"}]}。"""
    _require_slot("critic")
    sys_prompt = (
        "你是严苛的小说审校编辑。审校第%d章《%s》的初稿，逐项检查：\n"
        "1 一致性：与角色状态、世界观设定、前文摘要是否冲突；2 逻辑与衔接：因果、时间线、与上一章衔接；\n"
        "3 细纲覆盖：本章细纲要求的情节是否都写到；4 文风与 POV：是否符合文风规范；\n"
        "5 伏笔：该呼应的伏笔是否呼应；6 字数：实际约 %d 字，要求不少于 %d 字。\n"
        "通过标准：无一致性/逻辑硬伤、细纲基本覆盖、字数达标。问题要具体（指出哪里、为什么）。\n"
        "只输出 JSON：{\"pass\":true或false,\"score\":1到10的整数,\"issues\":[{\"type\":\"检查项\",\"detail\":\"问题描述\"}]}\n\n"
        "【作品上下文】\n%s"
        % (no, title, count_words(text), min_words, ctx_brief)
    )
    data = chat_json(sys_prompt, [{"role": "user", "content": "【待审校正文】\n" + text}],
                     slot="critic")
    issues = []
    for it in (data.get("issues") or []):
        if isinstance(it, dict) and it.get("detail"):
            issues.append({"type": str(it.get("type") or "其他").strip(),
                           "detail": str(it.get("detail") or "").strip()})
    try:
        score = int(data.get("score"))
    except (TypeError, ValueError):
        score = 0
    score = max(1, min(10, score))
    return {"pass": as_bool(data.get("pass")) and score >= 7, "score": score, "issues": issues}


def revise_chapter(ctx, title, text, issues, min_words):
    """按审校问题清单整体重写一章（generation 槽位），返回新正文。"""
    _require_slot("generation")
    issue_lines = "\n".join("- [%s] %s" % (i["type"], i["detail"]) for i in issues) or "- （无）"
    sys_prompt = (
        "你是网络小说作家，正在根据审校意见重写第%d章《%s》。\n"
        "要求：输出完整的重写后正文（不是修改说明、不是局部片段）；解决下面每一条审校问题；"
        "不少于 %d 字；保持文风规范与 POV 约束；只输出正文。\n\n"
        "【审校问题清单】\n%s\n\n【作品上下文】\n%s"
        % (ctx.get("no", 0), title, min_words, issue_lines, ctx["text"])
    )
    new_text = chat([{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": "【初稿】\n" + text}]).strip()
    new_text = _strip_junk(new_text, title)
    if not new_text:
        raise LLMError("模型返回的重写正文为空")
    return new_text


def extract_adoption(no, title, text, roster, open_foreshadows):
    """从定稿正文抽取采纳所需的全部事实（extraction 槽位）。

    返回 {"summary": str,
          "character_updates": [{"name","state":{键:值}}],   # 只含本章实际变化的状态键
          "foreshadow_updates": [{"id","status"}],            # 已有伏笔的状态变化
          "new_foreshadows": [{"content","plan_recycle"}]}    # 本章新埋的伏笔
    """
    _require_slot("extraction")
    roster_text = "、".join(roster) or "（无）"
    fore_text = "\n".join("- %s｜%s｜计划回收：%s" % (f["id"], f["content"], f.get("plan_recycle", ""))
                          for f in open_foreshadows) or "（无）"
    sys_prompt = (
        "你是小说事实抽取员。阅读第%d章《%s》定稿正文，抽取写作系统需要回写的事实。\n"
        "要求：\n"
        "1 summary：本章事实摘要 3~5 句（关键事件、人物状态与关系变化、新设定或伏笔），供记忆检索；\n"
        "2 character_updates：本章状态实际发生变化的角色，name 必须来自现有角色名单；"
        "state 只放发生变化的键（位置/身体状态/心理状态/当前目标/已知秘密/持有物·能力），没变化的角色不要列；\n"
        "3 foreshadow_updates：本章被回收或推进的已有伏笔，id 必须来自下方伏笔清单，status 取「已回收」或「待回收」；\n"
        "4 new_foreshadows：本章新埋下、需要后续回收的伏笔（没有就空数组），plan_recycle 写建议回收位置。\n"
        "不确定的不要编造。只输出 JSON：{\"summary\":\"\",\"character_updates\":[{\"name\":\"\",\"state\":{}}],"
        "\"foreshadow_updates\":[{\"id\":\"\",\"status\":\"\"}],\"new_foreshadows\":[{\"content\":\"\",\"plan_recycle\":\"\"}]}\n\n"
        "【现有角色名单】\n%s\n\n【未回收伏笔清单】\n%s"
        % (no, title, roster_text, fore_text)
    )
    data = chat_json(sys_prompt, [{"role": "user", "content": "【定稿正文】\n" + text}],
                     slot="extraction")
    return _clean_adoption(data)


def _clean_adoption(data):
    if not isinstance(data, dict):
        raise LLMError("模型返回缺少抽取结果")
    summary = str(data.get("summary") or "").strip()
    if not summary:
        raise LLMError("模型未返回有效摘要")
    char_updates = []
    for cu in (data.get("character_updates") or []):
        if not isinstance(cu, dict):
            continue
        name = str(cu.get("name") or "").strip()
        state = {str(k).strip(): str(v).strip()
                 for k, v in (cu.get("state") or {}).items() if str(v).strip()}
        if name and state:
            char_updates.append({"name": name, "state": state})
    fore_updates = []
    for fu in (data.get("foreshadow_updates") or []):
        if not isinstance(fu, dict):
            continue
        fid = str(fu.get("id") or "").strip()
        status = str(fu.get("status") or "").strip()
        if fid and status in ("已回收", "待回收", "长线"):
            fore_updates.append({"id": fid, "status": status})
    new_fores = []
    for nf in (data.get("new_foreshadows") or []):
        if not isinstance(nf, dict):
            continue
        content = str(nf.get("content") or "").strip()
        if content:
            new_fores.append({"content": content,
                              "plan_recycle": str(nf.get("plan_recycle") or "").strip()})
    return {"summary": summary, "character_updates": char_updates,
            "foreshadow_updates": fore_updates, "new_foreshadows": new_fores}
