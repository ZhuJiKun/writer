"""世界观 / 伏笔 / 文风 / 记忆 / 写作工作台五个模块的 LLM 生成能力。

每个函数都是一次 chat_json / chat 调用；未配置模型时抛 LLMError。
槽位：规划与正文用 generation，审校用 critic，采纳抽取用 extraction。
提示词文本统一在 llm/prompts.py。
"""

import json
import re

from llm import prompts
from llm.client import LLMError, as_bool, chat, chat_json, llm_available

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
    sys_prompt = prompts.p_init_bible(_project_brief(project))
    data = chat_json(sys_prompt, [{"role": "user", "content": "请生成初始设定库。"}])
    return _clean_entries(data)


def extend_bible_category(project, category, existing_names, hint=""):
    """给指定分类补充 1~3 条设定。返回 [{"category","name","content"}, ...]。"""
    _require_llm()
    names = "、".join(existing_names) or "（空）"
    sys_prompt = prompts.p_extend_bible_category(category, names, _project_brief(project))
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
    sys_prompt = prompts.p_brainstorm_foreshadows(_project_brief(project), _outline_brief(outline))
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
    data = chat_json(prompts.P_COMPRESS_WINDOW, [{"role": "user", "content": "\n".join(lines)}])
    summary = str(data.get("summary") or "").strip() if isinstance(data, dict) else ""
    if not summary:
        raise LLMError("模型未返回有效摘要")
    return summary


def extract_chapter_summary(title, text):
    """从章节正文抽取事实摘要（3-5 句）。返回摘要 str。供写作工作台生成正文后调用。"""
    _require_llm()
    data = chat_json(prompts.P_EXTRACT_CHAPTER_SUMMARY,
                     [{"role": "user", "content": f"章节标题：{title}\n\n{text}"}])
    summary = str(data.get("summary") or "").strip() if isinstance(data, dict) else ""
    if not summary:
        raise LLMError("模型未返回有效摘要")
    return summary


# ---------- 文风控制 ----------

def extract_style_constraints(style_prompt):
    """从 style_prompt 解析结构化约束。返回 {person, pov, tense, paragraph, dialogue_ratio}。"""
    _require_llm()
    data = chat_json(prompts.P_EXTRACT_STYLE_CONSTRAINTS,
                     [{"role": "user", "content": style_prompt}])
    return {k: str(data.get(k) or "").strip()
            for k in ("person", "pov", "tense", "paragraph", "dialogue_ratio")}


def generate_style_sample(project, style_prompt):
    """依据文风规范 + 作品概要生成一段标准样章。返回样章文本 str。"""
    _require_llm()
    sys_prompt = prompts.p_generate_style_sample(style_prompt, _project_brief(project))
    data = chat_json(sys_prompt, [{"role": "user", "content": "请生成样章。"}])
    sample = str(data.get("sample") or "").strip()
    if not sample:
        raise LLMError("模型返回的 sample 为空")
    return sample


# ---------- 写作工作台（规划 / 生成 / 审校 / 重写 / 采纳抽取） ----------

def _require_slot(slot):
    from llm.client import slot_available
    if not slot_available(slot):
        raise LLMError("未配置%s模型，请先到「模型配置」页检查" %
                       {"generation": "生成", "critic": "审校", "extraction": "抽取"}.get(slot, slot))


def plan_chapters(ctx, brief, count, start_no, vol_label):
    """根据上下文 + 用户概要规划 count 章细纲（generation 槽位）。

    ctx 为 write_engine 组装的上下文文本。返回 [{"title","summary"}, ...]，长度 == count。
    """
    _require_slot("generation")
    sys_prompt = prompts.p_plan_chapters(count, start_no, vol_label, ctx)
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
            {"role": "user", "content": prompts.p_plan_chapters_retry(last, count)},
        ]
    raise LLMError("模型两次返回的章数都不足（要 %d 章，得 %d 章），请换个说法重试" % (count, last or 0))


def generate_chapter(ctx, title, min_words, note=""):
    """生成一章正文（generation 槽位），返回正文字符串。"""
    _require_slot("generation")
    sys_prompt = prompts.p_generate_chapter(ctx.get("no", 0), title, min_words, ctx["text"])
    user = prompts.p_generate_chapter_user(note)
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
    sys_prompt = prompts.p_critic_review(no, title, count_words(text), min_words, ctx_brief)
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
    # scene_end 是顺带抽取的场景锚点，解析失败只丢弃、不影响审校结论
    return {"pass": as_bool(data.get("pass")) and score >= 7, "score": score,
            "issues": issues, "scene_end": clean_scene_end(data.get("scene_end"))}


def clean_scene_end(raw):
    """把模型返回的场景锚点规整为 {"time","place","present"}；三字段全空或非法返回 None。"""
    if not isinstance(raw, dict):
        return None
    present_raw = raw.get("present")
    if isinstance(present_raw, str):
        present_raw = present_raw.replace("、", ",").replace("；", ",").replace(";", ",").split(",")
    present = [str(p).strip() for p in (present_raw or []) if str(p).strip()]
    scene = {"time": str(raw.get("time") or "").strip(),
             "place": str(raw.get("place") or "").strip(),
             "present": present}
    if scene["time"] or scene["place"] or present:
        return scene
    return None


def revise_chapter(ctx, title, text, issues, min_words, note=""):
    """按审校问题清单整体重写一章（generation 槽位），返回新正文。重写仍需遵守用户的补充要求。"""
    _require_slot("generation")
    issue_lines = "\n".join("- [%s] %s" % (i["type"], i["detail"]) for i in issues) or "- （无）"
    note_block = ""
    if note:
        note_block = prompts.p_revise_note_block(note)
    sys_prompt = prompts.p_revise_chapter(ctx.get("no", 0), title, min_words,
                                          issue_lines, ctx["text"], note_block)
    new_text = chat([{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": "【初稿】\n" + text}]).strip()
    new_text = _strip_junk(new_text, title)
    if not new_text:
        raise LLMError("模型返回的重写正文为空")
    return new_text


def gen_chapter_meta(text, cur_title, cur_summary, requirement=""):
    """根据章节正文生成标题与概要（generation 槽位）。返回 {"title","summary"}。

    用于写作工作台：用户对已生成正文重起名/写概要；requirement 为用户的可选补充要求。
    """
    _require_slot("generation")
    user = ("【当前标题】%s\n【当前概要】%s\n\n【章节正文】\n%s"
            % (cur_title or "（无）", cur_summary or "（无）", text))
    if requirement:
        user += prompts.p_gen_chapter_meta_requirement(requirement)
    data = chat_json(prompts.P_GEN_CHAPTER_META, [{"role": "user", "content": user}])
    title = str(data.get("title") or "").strip() if isinstance(data, dict) else ""
    summary = str(data.get("summary") or "").strip() if isinstance(data, dict) else ""
    if not title or not summary:
        raise LLMError("模型未返回有效的标题/概要，请重试")
    return {"title": title, "summary": summary}


def revise_content(title, text, instruction):
    """按用户要求局部微调章节正文（generation 槽位），返回完整的修改后正文。

    用于「整体满意、只改某些句子」的场景：不动其余内容，也不走审校。
    """
    _require_slot("generation")
    sys_prompt = prompts.p_revise_content(title, instruction)
    new_text = chat([{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": "【原正文】\n" + text}]).strip()
    new_text = _strip_junk(new_text, title)
    if not new_text:
        raise LLMError("模型返回的修改稿为空")
    if count_words(new_text) < max(200, count_words(text) // 2):
        raise LLMError("模型返回的修改稿篇幅异常（远短于原文），请换个说法重试")
    return new_text


def extract_adoption(no, title, text, roster, open_foreshadows):
    """从定稿正文抽取采纳所需的全部事实（extraction 槽位）。

    返回 {"summary": str,
          "character_updates": [{"name","state":{键:值}}],   # 只含本章实际变化的状态键
          "foreshadow_updates": [{"id","status"}],            # 已有伏笔的状态变化
          "new_foreshadows": [{"content","plan_recycle"}],    # 本章新埋的伏笔
          "scene_end": {"time","place","present"} | None}     # 本章结尾场景锚点
    """
    _require_slot("extraction")
    roster_text = "、".join(roster) or "（无）"
    fore_text = "\n".join("- %s｜%s｜计划回收：%s" % (f["id"], f["content"], f.get("plan_recycle", ""))
                          for f in open_foreshadows) or "（无）"
    sys_prompt = prompts.p_extract_adoption(no, title, roster_text, fore_text)
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
            "foreshadow_updates": fore_updates, "new_foreshadows": new_fores,
            "scene_end": clean_scene_end(data.get("scene_end"))}


def extract_scene_end(no, title, text):
    """单独从正文抽取结尾场景锚点（extraction 槽位），供旧章节补抽用。

    返回 {"time","place","present"}；抽不到有效内容抛 LLMError。
    """
    _require_slot("extraction")
    sys_prompt = prompts.p_extract_scene_end(no, title)
    data = chat_json(sys_prompt, [{"role": "user", "content": "【正文】\n" + text}],
                     slot="extraction")
    scene = clean_scene_end(data.get("scene_end") if isinstance(data, dict) else None) \
        or clean_scene_end(data)
    if not scene:
        raise LLMError("模型未能从正文抽取出场景锚点")
    return scene
