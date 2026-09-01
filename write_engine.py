"""写作流水线编排：上下文组装、逐章「生成→审校→重写」、采纳回写，以及内存后台任务表。

上下文组装原则（对应各记忆与设定模块的职责）：
- 作品档案 / 文风规范：全书恒定，每次必带
- 全书主线 + 本卷摘要 + 相邻章细纲：定位本章在大纲中的位置
- 角色状态（动态）：只带非空状态项，防止 OOC
- 世界观设定库：全量注入（条目都是短句，成本可控）
- 未回收伏笔：提醒正文呼应
- 分层记忆：合并摘要全量（远期）+ 最近 5 条逐章摘要（中期）+ 最近一章正文结尾（近期）
"""

import threading
import uuid
from datetime import datetime

import bible_store as bst
import chapters_store as hst
import character_store as cst
import content_llm as cli
import foreshadow_store as fst
import memory_store as mst
import outline_store as ost
import project_store as ps
import style_store as sst
from llm_client import LLMError

MAX_REVISE_ROUNDS = 2      # 审校不通过时的最大重写次数（共最多 3 版正文）
RECENT_FULL_CHARS = 3000   # 注入的上一章正文最大字符（取结尾部分）
RECENT_SUMMARIES = 5       # 注入的逐章摘要条数
DEFAULT_MIN_WORDS = 1500   # 无批次章节（大纲里原有的待生成章）重新生成时的默认字数

TASKS = {}
_LOCK = threading.Lock()
MAX_FINISHED_KEPT = 20  # 已结束任务最多保留的条数（超出按结束时间淘汰最旧的）


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------- 后台任务 ----------

def get_task(tid):
    return TASKS.get(tid)


def active_task():
    """正在运行中的任务 id（同时只允许一个写作/采纳任务）。"""
    with _LOCK:
        for tid, t in TASKS.items():
            if t.get("status") == "running":
                return tid
    return None


def _new_task_locked(kind, total):
    """在 _LOCK 内调用：淘汰最旧的已结束任务并创建新任务。"""
    tid = "t_" + uuid.uuid4().hex[:8]
    finished = sorted((t for t in TASKS.values() if t.get("status") != "running"),
                      key=lambda t: t.get("finished_at") or "")
    for t in finished[:-MAX_FINISHED_KEPT]:
        TASKS.pop(t["id"], None)
    TASKS[tid] = {"id": tid, "kind": kind, "status": "running",
                  "total": total, "done": 0, "fail": 0, "current": "",
                  "logs": [], "started_at": _now(), "finished_at": ""}
    return tid


def reserve_task(kind, total):
    """原子地完成「检查无运行任务 + 占用任务槽」（同一把锁内），已有任务运行时返回 None。

    占槽成功后必须 launch_task 启动线程；若启动前的数据写入失败，调 abort_task 释放槽位。
    """
    with _LOCK:
        for t in TASKS.values():
            if t.get("status") == "running":
                return None
        return _new_task_locked(kind, total)


def launch_task(tid, kind, chapter_ids):
    """为已占槽的任务启动后台线程。"""
    threading.Thread(target=_run_guarded, args=(tid, kind, chapter_ids), daemon=True).start()


def abort_task(tid, reason=""):
    """占槽后、启动前失败：记录原因并落到失败终态，释放任务槽。"""
    if reason:
        _log(tid, reason)
    _finish(tid, "error")


def start_generate(chapter_ids):
    """启动生成任务（章节按全局章号顺序处理）。已有任务运行时返回 None。"""
    tid = reserve_task("generate", len(chapter_ids))
    if tid is None:
        return None
    launch_task(tid, "generate", chapter_ids)
    return tid


def start_adopt(chapter_ids):
    """启动采纳任务。已有任务运行时返回 None。"""
    tid = reserve_task("adopt", len(chapter_ids))
    if tid is None:
        return None
    launch_task(tid, "adopt", chapter_ids)
    return tid


def _log(tid, line):
    t = TASKS.get(tid)
    if t is None:
        return
    t["logs"].append("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), line))
    if len(t["logs"]) > 300:
        t["logs"] = t["logs"][-300:]


def _finish(tid, status):
    t = TASKS.get(tid)
    if t:
        t["status"] = status
        t["current"] = ""
        t["finished_at"] = _now()


def _run_guarded(tid, kind, chapter_ids):
    try:
        if kind == "generate":
            _run_generate(tid, chapter_ids)
        else:
            _run_adopt(tid, chapter_ids)
        t = TASKS.get(tid)
        # 有任一章节失败即标记 error（前端红徽章「有失败」），全败时不再显示绿色「已完成」
        _finish(tid, "done" if t and t.get("done") and not t.get("fail") else "error")
    except Exception as e:  # 兜底：单章异常已在流水线内捕获，这里是系统性错误
        _log(tid, "任务中断：%s" % e)
        _finish(tid, "error")


# ---------- 上下文组装 ----------

def chapter_map():
    """{章节id: (卷, 章节, 全局章号)}"""
    return {ch["id"]: (v, ch, no) for v, ch, no in ost.iter_chapters()}


def _project_block(proj):
    bg = proj.get("background", {})
    return (
        "【作品档案】\n"
        "书名：%s\n题材：%s\n一句话梗概：%s\n故事概要：%s\n"
        "时代/世界：%s\n规则/力量体系：%s\n舞台/势力：%s"
        % (proj.get("title") or "（未定）", proj.get("genre"), proj.get("logline"),
           proj.get("synopsis"), bg.get("era"), bg.get("rules"), bg.get("stage"))
    )


def _style_block(proj):
    lines = []
    if proj.get("style_prompt"):
        lines.append("文风规范：" + proj["style_prompt"])
    cons = {k: v for k, v in sst.load_store()["constraints"].items() if v}
    if cons:
        label = {"person": "人称", "pov": "视角", "tense": "时态",
                 "paragraph": "段落", "dialogue_ratio": "对话比例"}
        lines.append("结构约束：" + "；".join("%s=%s" % (label.get(k, k), v) for k, v in cons.items()))
    samples = sst.load_store()["samples"]
    if samples:
        lines.append("文风样例（模仿其语感）：\n" + samples[-1].get("content", "")[:400])
    return "【文风规范】\n" + "\n".join(lines) if lines else ""


def _character_block():
    lines = []
    for c in cst.load_chars():
        head = "%s（%s%s）" % (c.get("name", ""), c.get("role_type", ""),
                               "/" + c["gender"] if c.get("gender") else "")
        if c.get("personality"):
            head += "：" + c["personality"][:120]
        states = ["%s=%s" % (k, v) for k, v in (c.get("state") or {}).items() if v]
        line = "- " + head
        if states:
            line += "\n  当前状态：" + "；".join(states)
        lines.append(line)
    return "【角色状态】\n" + "\n".join(lines) if lines else ""


def _bible_block():
    lines = ["【%s】%s：%s" % (e.get("category", ""), e.get("name", ""), e.get("content", ""))
             for e in bst.load_store()["entries"]]
    return "【世界观设定】\n" + "\n".join(lines) if lines else ""


def _foreshadow_block():
    lines = ["- %s｜%s（埋设：%s；计划回收：%s）"
             % (it["id"], it.get("content", ""), it.get("planted", ""), it.get("plan_recycle", ""))
             for it in fst.load_store()["items"] if it.get("status") != "已回收"]
    return "【待回收伏笔】\n" + "\n".join(lines) if lines else ""


def _memory_block(cur_no, cmap):
    """分层记忆：合并摘要（远期）+ 最近逐章摘要（中期）+ 上一章正文结尾（近期）。"""
    store = mst.load_store()
    parts = []
    merged = sorted(store["merged_summaries"],
                    key=lambda m: (m.get("from_no") or 0, m.get("to_no") or 0))
    if merged:
        parts.append("【远期剧情·合并摘要】\n" + "\n".join(
            "%s：%s" % (m.get("range", ""), m.get("summary", "")) for m in merged))
    recent = [c for c in store["chapter_summaries"] if 0 < c.get("no", 0) < cur_no]
    recent.sort(key=lambda c: c["no"])
    if recent:
        parts.append("【近期剧情·逐章摘要】\n" + "\n".join(
            "第%d章《%s》：%s" % (c["no"], c.get("title", ""), c.get("summary", ""))
            for c in recent[-RECENT_SUMMARIES:]))
    # 最近一章有正文的章节（草稿或已采纳均可），取结尾部分保持行文连贯
    entries = hst.all_entries()
    best = None  # (no, title, content)
    for cid, e in entries.items():
        if not e.get("content"):
            continue
        meta = cmap.get(cid)
        if not meta:
            continue
        _, ch, no = meta
        if no < cur_no and (best is None or no > best[0]):
            best = (no, ch.get("title", ""), e["content"])
    if best:
        parts.append("【上一章正文·结尾节选】\n第%d章《%s》：\n……%s"
                     % (best[0], best[1], best[2][-RECENT_FULL_CHARS:]))
    return parts


def build_chapter_context(cid, cmap=None):
    """组装单章正文生成所需的完整上下文。返回 {no, vol, title, summary, text}。"""
    cmap = cmap or chapter_map()
    if cid not in cmap:
        raise LLMError("章节不存在（可能已在大纲中被删除）")
    vol, ch, no = cmap[cid]
    proj = ps.load_project()
    parts = [_project_block(proj)]
    for block in (_style_block(proj), _character_block(), _bible_block(), _foreshadow_block()):
        if block:
            parts.append(block)
    outline = ost.load_store()
    outline_lines = ["全书主线：" + (outline.get("main") or "（未定）"),
                     "本卷：%s——%s" % (vol.get("title", ""), vol.get("summary") or "")]
    # 相邻章节细纲（全书顺序上的前后各一章）
    ordered = sorted(cmap.items(), key=lambda kv: kv[1][2])
    for other_cid, (_, other_ch, other_no) in ordered:
        if other_no == no - 1:
            outline_lines.append("上一章细纲：第%d章《%s》——%s"
                                 % (other_no, other_ch.get("title", ""), other_ch.get("summary", "")))
        elif other_no == no + 1:
            outline_lines.append("下一章细纲：第%d章《%s》——%s"
                                 % (other_no, other_ch.get("title", ""), other_ch.get("summary", "")))
    parts.append("【大纲定位】\n" + "\n".join(outline_lines))
    parts.extend(_memory_block(no, cmap))
    parts.append("【本章细纲】\n第%d章《%s》：%s" % (no, ch.get("title", ""), ch.get("summary", "")))
    return {"no": no, "vol": vol.get("title", ""), "title": ch.get("title", ""),
            "summary": ch.get("summary", ""), "text": "\n\n".join(parts)}


def build_plan_context():
    """组装批次细纲规划所需的上下文文本（比单章上下文多全卷细纲，不带上一章正文）。"""
    proj = ps.load_project()
    parts = [_project_block(proj)]
    parts.append(cli._outline_brief(ost.load_store()))
    for block in (_character_block(), _bible_block(), _foreshadow_block()):
        if block:
            parts.append(block)
    store = mst.load_store()
    merged = sorted(store["merged_summaries"],
                    key=lambda m: (m.get("from_no") or 0, m.get("to_no") or 0))
    if merged:
        parts.append("【远期剧情·合并摘要】\n" + "\n".join(
            "%s：%s" % (m.get("range", ""), m.get("summary", "")) for m in merged))
    recent = sorted(store["chapter_summaries"], key=lambda c: c.get("no", 0))
    if recent:
        parts.append("【近期剧情·逐章摘要】\n" + "\n".join(
            "第%d章《%s》：%s" % (c["no"], c.get("title", ""), c.get("summary", ""))
            for c in recent[-RECENT_SUMMARIES:]))
    return "\n\n".join(parts)


# ---------- 生成流水线 ----------

def _run_generate(tid, chapter_ids):
    cmap = chapter_map()
    task = TASKS[tid]
    ok = fail = 0
    for i, cid in enumerate(chapter_ids):
        meta = cmap.get(cid)
        if not meta:
            _log(tid, "章节 %s 不在大纲中，跳过" % cid)
            task["done"] = i + 1
            fail += 1
            continue
        _, ch, no = meta
        label = "第%d章《%s》" % (no, ch.get("title", ""))
        entry = hst.get_entry(cid) or hst.upsert_entry(hst.new_entry(cid))
        min_words = entry.get("min_words") or DEFAULT_MIN_WORDS
        note = entry.get("note", "")
        try:
            ctx = build_chapter_context(cid, cmap)
        except LLMError as e:
            hst.set_status(cid, hst.ST_FAILED, error=str(e))
            _log(tid, "%s：上下文组装失败 — %s" % (label, e))
            fail += 1
            task["done"] = i + 1
            continue
        # 第一步：生成正文。只有这一步失败才算章节失败
        task["current"] = label + " · 生成正文"
        hst.set_status(cid, hst.ST_GENERATING)
        _log(tid, "%s：开始生成正文（要求 ≥%d 字）" % (label, min_words))
        try:
            text = cli.generate_chapter(ctx, ch["title"], min_words, note)
        except LLMError as e:
            hst.set_status(cid, hst.ST_FAILED, error="正文生成失败：" + str(e))
            _log(tid, "%s：正文生成失败 — %s" % (label, e))
            fail += 1
            task["done"] = i + 1
            continue
        # 正文是昂贵产物：先落盘再审校；审校/重写失败都保留正文为草稿
        hst.set_status(cid, hst.ST_REVIEWING, content=text, word_count=cli.count_words(text))
        review = {"rounds": 0, "final": None, "history": []}
        passed = False
        review_err = ""
        try:
            for round_no in range(MAX_REVISE_ROUNDS + 1):
                task["current"] = label + " · 审校（第 %d 轮）" % (round_no + 1)
                hst.set_status(cid, hst.ST_REVIEWING)
                result = cli.critic_review(ctx["text"], no, ch["title"], text, min_words)
                wc = cli.count_words(text)
                result["word_count"] = wc
                # 字数不足也记入审校历史，保证「记录页可见的问题」与「驱动重写的问题」一致
                if wc < min_words:
                    result["issues"].append({"type": "字数",
                                             "detail": "实际约 %d 字，不足要求的 %d 字" % (wc, min_words)})
                review["history"].append({"round": round_no + 1, **result})
                review["rounds"] = round_no + 1
                _log(tid, "%s：第 %d 轮审校 %s（%d 分，约 %d 字，%d 个问题）"
                     % (label, round_no + 1, "通过" if result["pass"] else "未通过",
                        result["score"], wc, len(result["issues"])))
                issues = result["issues"]
                if result["pass"] and wc >= min_words:
                    passed = True
                    break
                if round_no < MAX_REVISE_ROUNDS:
                    task["current"] = label + " · 按审校意见重写"
                    hst.set_status(cid, hst.ST_GENERATING)
                    try:
                        text = cli.revise_chapter(ctx, ch["title"], text, issues, min_words)
                    except LLMError as e:
                        review_err = "重写失败：" + str(e)
                        _log(tid, "%s：重写失败，保留上一版正文 — %s" % (label, e))
                        break
                    hst.set_status(cid, hst.ST_REVIEWING, content=text, word_count=cli.count_words(text))
        except LLMError as e:
            review_err = "审校调用失败：" + str(e)
            _log(tid, "%s：审校调用失败（正文已保留为草稿）— %s" % (label, e))
        if review["history"]:
            review["final"] = review["history"][-1]
        hst.set_status(cid, hst.ST_DRAFT, content=text, word_count=cli.count_words(text),
                       review=review, error=review_err)
        ok += 1
        if passed:
            _log(tid, "%s：完成（约 %d 字）" % (label, cli.count_words(text)))
        elif review_err:
            _log(tid, "%s：%s（可在列表中勾选后重新生成）" % (label, review_err))
        else:
            _log(tid, "%s：审校未完全通过，已保留最佳版本为草稿（可在列表中重新生成）" % label)
        task["done"] = i + 1
    task["fail"] = fail
    _refresh_batches()
    _log(tid, "任务结束：成功 %d 章，失败 %d 章" % (ok, fail))


def _refresh_batches():
    """批次内没有排队/进行中章节时，把批次标记为 done。"""
    entries = hst.all_entries()
    for b in hst.load_store()["batches"]:
        if b.get("status") != "confirmed":
            continue
        active = (hst.ST_PLANNED, hst.ST_GENERATING, hst.ST_REVIEWING)
        if all((entries.get(cid) or {}).get("status") not in active
               for cid in b.get("chapter_ids", [])):
            hst.update_batch(b["id"], status="done")


# ---------- 采纳流水线 ----------

def _run_adopt(tid, chapter_ids):
    cmap = chapter_map()
    task = TASKS[tid]
    ok = fail = 0
    for i, cid in enumerate(chapter_ids):
        meta = cmap.get(cid)
        entry = hst.get_entry(cid)
        if not meta or not entry or entry.get("status") != hst.ST_DRAFT or not entry.get("content"):
            _log(tid, "章节 %s 状态不可采纳（仅草稿可采纳），跳过" % cid)
            task["done"] = i + 1
            fail += 1
            continue
        vol, ch, no = meta
        label = "第%d章《%s》" % (no, ch.get("title", ""))
        task["current"] = label + " · 事实抽取与回写"
        roster = [c.get("name", "") for c in cst.load_chars() if c.get("name")]
        open_fores = [{"id": it["id"], "content": it.get("content", ""),
                       "plan_recycle": it.get("plan_recycle", "")}
                      for it in fst.load_store()["items"] if it.get("status") != "已回收"]
        try:
            data = cli.extract_adoption(no, ch.get("title", ""), entry["content"],
                                        roster, open_fores)
            changes = _apply_adoption(cid, no, vol.get("title", ""), ch.get("title", ""),
                                      entry, data)
            hst.set_status(cid, hst.ST_ADOPTED, adopted_at=_now(), note="")
            ok += 1
            _log(tid, "%s：已采纳（%s）" % (label, "；".join(changes)))
        except LLMError as e:
            _log(tid, "%s：采纳失败 — %s" % (label, e))
            fail += 1
        task["done"] = i + 1
    task["fail"] = fail
    _log(tid, "采纳结束：成功 %d 章，失败 %d 章" % (ok, fail))


def _apply_adoption(cid, no, vol_title, title, entry, data):
    """把抽取结果回写到记忆 / 角色 / 伏笔 / 大纲，返回变更描述列表。"""
    changes = []
    # 1. 分层记忆：逐章摘要升级为正文来源
    mst.upsert_chapter_summary(cid, no, vol_title, title, data["summary"], mst.SOURCE_TEXT)
    changes.append("摘要已写入逐章摘要层")
    # 2. 角色状态：只更新发生变化的键。重名角色不回写（按名匹配无法区分，宁缺毋错）。
    # 实际「读-改-写」在 cst.apply_state_updates 的锁内完成，避免与手动编辑互相覆盖。
    chars = [c for c in cst.load_chars() if c.get("name")]
    name_count = {}
    for c in chars:
        name_count[c["name"]] = name_count.get(c["name"], 0) + 1
    by_name = {c["name"]: c for c in chars if name_count[c["name"]] == 1}
    n_state = 0
    skipped_dup = set()
    note = "第%d章《%s》采纳" % (no, title)
    for cu in data["character_updates"]:
        if name_count.get(cu["name"], 0) > 1:
            skipped_dup.add(cu["name"])
            continue
        char = by_name.get(cu["name"])
        if not char:
            continue
        n_state += cst.apply_state_updates(char["id"], cu["state"], note)
    if n_state:
        changes.append("角色状态更新 %d 项" % n_state)
    for name in sorted(skipped_dup):
        changes.append("角色「%s」重名，状态未回写" % name)
    # 3. 伏笔：状态变更 + 新埋伏笔（内容完全相同的去重）
    n_fore = 0
    items = fst.load_store()["items"]
    for fu in data["foreshadow_updates"]:
        cur = next((it for it in items if it.get("id") == fu["id"]), None)
        if cur and cur.get("status") != fu["status"]:
            fst.update_item(fu["id"], cur.get("content", ""), cur.get("planted", ""),
                            cur.get("plan_recycle", ""), fu["status"])
            n_fore += 1
    existing = {it.get("content", "") for it in fst.load_store()["items"]}
    for nf in data["new_foreshadows"]:
        if nf["content"] and nf["content"] not in existing:
            fst.add_item(nf["content"], planted="第%d章《%s》" % (no, title),
                         plan_recycle=nf.get("plan_recycle", ""), status="待回收")
            existing.add(nf["content"])
            n_fore += 1
    if n_fore:
        changes.append("伏笔更新/新增 %d 条" % n_fore)
    # 4. 大纲：状态与字数
    ost.update_chapter(cid, status=ost.STATUS_DONE, word_count=entry.get("word_count", 0))
    changes.append("大纲标记已生成")
    return changes
