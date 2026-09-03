import difflib
import json
import urllib.error
import urllib.request

from flask import Flask, Response, jsonify, redirect, render_template, request, stream_with_context, url_for
from markupsafe import Markup, escape

from stores import bible_store as bst
from stores import chapters_store as hst
from stores import character_store as cst
from stores import chat_store as kst
from stores import config_store as cs
from stores import foreshadow_store as fst
from stores import memory_store as mst
from stores import outline_store as ost
from stores import project_store as ps
from stores import style_store as sst
from llm import character as cl
from llm import client as lc
from llm import content as cli
from llm import prompts as prm
import write_engine as we

app = Flask(__name__)


@app.template_filter("nl2br")
def nl2br(s):
    """转义后把换行变成 <br>，用于渲染 LLM/用户的多行文本（防注入）。
    注意要对转义后的纯字符串做 replace——Markup.replace 会把替换内容也转义。"""
    return Markup(str(escape(s or "")).replace("\n", "<br>\n"))


def _outline_stats():
    """(各卷进度列表, 总章数, 已生成章数)。供总览页与伏笔模块复用。"""
    volumes = ost.load_store()["volumes"]
    arcs = []
    total = done = 0
    for v in volumes:
        chs = v.get("chapters", [])
        v_done = sum(1 for ch in chs if ch.get("status") == ost.STATUS_DONE)
        total += len(chs)
        done += v_done
        if chs and v_done == len(chs):
            status = "已完成"
        elif v_done:
            status = "进行中"
        else:
            status = "未开始"
        arcs.append({"name": v.get("title", ""), "summary": v.get("summary", ""), "status": status})
    return arcs, total, done


@app.route("/")
def dashboard():
    proj = ps.load_project()
    arcs, total_ch, done_ch = _outline_stats()
    book = {
        "title": proj.get("title") or "（尚未建档）",
        "genre": proj.get("genre") or "—",
        "volumes": len(arcs),
        "current_chapter": done_ch,
        "total_planned": total_ch,
    }
    merged = mst.load_store()["merged_summaries"]
    # 按剧情顺序（覆盖范围的末章号）取最新 3 条，而非压缩落盘的插入序
    merged = sorted(merged, key=lambda m: (m.get("to_no") or 0, m.get("from_no") or 0))
    summaries = list(reversed(merged[-3:]))  # 最新 3 条合并摘要
    alerts = []
    for it in fst.due_items(done_ch):
        alerts.append(f"{it['id']}「{it['content']}」已到计划回收点（{it['plan_recycle']}），仍待回收")
    todo = sum(1 for it in fst.load_store()["items"] if it.get("status") == "待回收")
    if todo:
        alerts.append(f"伏笔追踪：共 {todo} 条待回收")
    return render_template("dashboard.html", book=book, arcs=arcs, alerts=alerts,
                           summaries=summaries, project=proj, wizard_done=ps.wizard_done())


# ---------- 写作工作台模块 ----------

MAX_BATCH_CHAPTERS = 20    # 单批最多生成章数（超出质量无法保证）
MIN_WORDS_FLOOR = 200      # 每章最小字数下限
CHAPTER_DEFAULT_SIZE = 5   # 章节列表默认每页章数
CHAPTER_PAGE_SIZES = (5, 10, 20, 50)


def _page_size_arg():
    """每页章数：只允许白名单取值，非法值回落默认。"""
    try:
        size = int(request.args.get("size", CHAPTER_DEFAULT_SIZE))
    except (TypeError, ValueError):
        return CHAPTER_DEFAULT_SIZE
    return size if size in CHAPTER_PAGE_SIZES else CHAPTER_DEFAULT_SIZE


def _chapter_rows():
    """工作台章节列表行：大纲章节 × 正文记录 的合并视图。"""
    entries = hst.all_entries()
    rows = []
    for v, ch, no in ost.iter_chapters():
        e = entries.get(ch["id"])
        e_status = e.get("status") if e else ""
        review = (e.get("review") or {}).get("final") if e else None
        rows.append({
            "cid": ch["id"], "no": no, "vol": v.get("title", ""),
            "title": ch.get("title", ""), "summary": ch.get("summary", ""),
            "outline_status": ch.get("status", ost.STATUS_TODO),
            "e_status": e_status,
            "status_label": hst.STATUS_LABELS.get(e_status, "") if e else ch.get("status", ""),
            "word_count": (e or {}).get("word_count") or ch.get("word_count") or 0,
            "review": review, "error": (e or {}).get("error", ""),
            "scene_end": (e or {}).get("scene_end"),
            "has_entry": bool(e),
            # 可勾选重新生成：有记录且处于 排队/失败/草稿；或大纲里原有的待生成章
            "regen_ok": (e and e_status in hst.REGENERABLE) or
                        (not e and ch.get("status") == ost.STATUS_TODO),
            "adopt_ok": bool(e and e_status == hst.ST_DRAFT),
            "read_ok": bool(e and e.get("content")),
            # 已采纳章节的摘要已回写分层记忆，标题/概要锁定；其余阶段都允许在工作台改
            "edit_ok": e_status != hst.ST_ADOPTED,
        })
    return rows


@app.route("/chapter")
def chapter():
    store = ost.load_store()
    hst.remove_orphans([ch["id"] for _, ch, _ in ost.iter_chapters(store)])
    all_rows = list(reversed(_chapter_rows()))  # 倒序：最新章节排在最前
    total = len(all_rows)
    size = _page_size_arg()
    pages = max(1, -(-total // size))
    page = min(_page_arg("page"), pages)
    rows = all_rows[(page - 1) * size:page * size]
    pending = hst.planned_batches()
    pending = pending[-1] if pending else None   # 同时只处理一个待确认方案
    pending_vol_label, pending_start_no = "", 0
    if pending:
        pending_start_no = total + 1
        if pending.get("vol_id"):
            vol = ost.get_volume(pending["vol_id"])
            pending_vol_label = vol.get("title", "") if vol else "（卷已被删除，确认时将自动新建一卷）"
        else:
            pending_vol_label = pending.get("new_vol_title") or "新建一卷"
    task = we.get_task(request.args.get("task", "")) or None
    if task is None:
        active = we.active_task()
        task = we.get_task(active) if active else None
    return render_template("chapter.html", rows=rows, total=total,
                           page=page, pages=pages, size=size, page_sizes=CHAPTER_PAGE_SIZES,
                           pending=pending,
                           pending_vol_label=pending_vol_label, pending_start_no=pending_start_no,
                           volumes=store["volumes"], next_no=total + 1,
                           max_batch=MAX_BATCH_CHAPTERS, task=task,
                           llm_on=lc.llm_available(), wizard_done=ps.wizard_done(),
                           msg=request.args.get("msg"), err=request.args.get("err"))


@app.route("/chapter/plan", methods=["POST"])
def chapter_plan():
    """用户概要 → LLM 规划 N 章细纲（1 次 LLM 调用），存为待确认批次。"""
    if not lc.llm_available():
        return redirect(url_for("chapter", err="未配置生成模型，请先到「模型配置」页填写"))
    brief = request.form.get("brief", "").strip()
    if not brief:
        return redirect(url_for("chapter", err="请先填写本批章节的内容概要"))
    try:
        count = int(request.form.get("count", 5))
    except ValueError:
        count = 5
    count = max(1, min(MAX_BATCH_CHAPTERS, count))
    try:
        min_words = int(request.form.get("min_words", 1500) or 1500)
        max_words = int(request.form.get("max_words", 3000) or 3000)
    except ValueError:
        min_words, max_words = 1500, 3000
    if min_words < MIN_WORDS_FLOOR:
        return redirect(url_for("chapter", err="每章最小字数不能低于 %d" % MIN_WORDS_FLOOR))
    if max_words < min_words:
        return redirect(url_for("chapter", err="最多字数不能小于最少字数"))
    vol_sel = request.form.get("vol_sel", "").strip()
    new_vol_title = request.form.get("new_vol_title", "").strip()
    volumes = ost.load_store()["volumes"]
    if vol_sel == "__new__" or not volumes:
        vol_id, vol_label = None, new_vol_title or "第%d卷" % (len(volumes) + 1)
    else:
        vol_id = vol_sel
        vol = ost.get_volume(vol_id)
        if not vol:
            return redirect(url_for("chapter", err="目标卷不存在"))
        vol_label = vol.get("title", "")
    # 同批只允许一个待确认方案：清掉旧的
    for b in hst.planned_batches():
        hst.delete_batch(b["id"])
    start_no = sum(len(v.get("chapters", [])) for v in ost.load_store()["volumes"]) + 1
    try:
        plan = cli.plan_chapters(we.build_plan_context(), brief, count, start_no, vol_label)
    except cli.LLMError as e:
        return redirect(url_for("chapter", err="细纲规划失败：" + str(e)))
    batch = hst.new_batch(brief, count, min_words, max_words, vol_id, vol_label if vol_id is None else "", plan)
    return redirect(url_for("chapter", msg="细纲方案已生成，请逐章核对（可直接修改标题与细纲）后确认")
                    + "#plan")


@app.route("/chapter/batch/<bid>/confirm", methods=["POST"])
def chapter_confirm(bid):
    """确认细纲：写入大纲并启动后台逐章生成。"""
    batch = hst.get_batch(bid)
    if not batch or batch.get("status") != "planned":
        return redirect(url_for("chapter", err="批次不存在或已确认"))
    titles = request.form.getlist("plan_title")
    summaries = request.form.getlist("plan_summary")
    notes = request.form.getlist("plan_note")
    batch_note = request.form.get("note", "").strip()
    plan, row_notes = [], []
    # 逐行对齐标题/细纲/本章补充（三者同名按序提交），标题或细纲为空的行丢弃
    for t, s, n in zip(titles, summaries, notes + [""] * len(titles)):
        t, s, n = t.strip(), s.strip(), n.strip()
        if t and s:
            plan.append({"title": t, "summary": s})
            row_notes.append(n)
    if not plan:
        return redirect(url_for("chapter", err="细纲不能为空（每章都需要标题与细纲）"))
    # 先原子占住任务槽再写盘：并发重复提交时，后到的请求在这里就会被拦下，
    # 不会把重复章节写进大纲（旧顺序是「先落盘后启动」，竞态下会留 planned 孤儿章节）
    tid = we.reserve_task("generate", len(plan))
    if tid is None:
        return redirect(url_for("chapter", err="已有任务在运行，请等它结束后再确认"))
    try:
        # 目标卷：沿用批次设定；卷被删了或选择新卷时自动建卷
        vol = ost.get_volume(batch.get("vol_id") or "")
        if vol is None:
            title = batch.get("new_vol_title") or "第%d卷" % (len(ost.load_store()["volumes"]) + 1)
            vol = ost.add_volume(title, batch.get("brief", "")[:80])
        chapter_ids = []
        for item, chapter_note in zip(plan, row_notes):
            ch = ost.add_chapter(vol["id"], item["title"], item["summary"], status=ost.STATUS_TODO)
            # 批次补充 + 本章补充合并为该章 entry 的 note，生成/重写时随 prompt 传给 LLM
            parts = []
            if batch_note:
                parts.append("【全批要求】" + batch_note)
            if chapter_note:
                parts.append("【本章要求】" + chapter_note)
            hst.upsert_entry(hst.new_entry(ch["id"], batch_id=bid,
                                           min_words=batch.get("min_words", 1500),
                                           max_words=batch.get("max_words", 0),
                                           note="\n".join(parts)))
            chapter_ids.append(ch["id"])
        hst.update_batch(bid, plan=plan, chapter_ids=chapter_ids, vol_id=vol["id"],
                         note=batch_note, status="confirmed")
    except Exception as e:
        we.abort_task(tid, "细纲落盘失败：%s" % e)
        return redirect(url_for("chapter", err="细纲落盘失败：" + str(e)))
    we.launch_task(tid, "generate", chapter_ids)
    return redirect(url_for("chapter", task=tid))


@app.route("/chapter/batch/<bid>/discard", methods=["POST"])
def chapter_discard(bid):
    batch = hst.get_batch(bid)
    if not batch or batch.get("status") != "planned":
        return redirect(url_for("chapter", err="批次不存在或已确认，不能放弃"))
    hst.delete_batch(bid)
    return redirect(url_for("chapter", msg="已放弃该细纲方案"))


def _sorted_ids(ids):
    """按全局章号排序章节 id 列表（生成与采纳都按剧情顺序处理）；重复提交的 id 去重。"""
    order = {ch["id"]: no for _, ch, no in ost.iter_chapters()}
    return sorted((i for i in dict.fromkeys(ids) if i in order), key=lambda i: order[i])


@app.route("/chapter/<cid>/meta", methods=["POST"])
def chapter_meta_update(cid):
    """工作台直接改章节标题/概要：未采纳的章节随时可改；已采纳的锁定（摘要已回写记忆）。"""
    vol, ch, no = ost.find_chapter(cid)
    if not ch:
        return redirect(url_for("chapter", err="章节不存在"))
    entry = hst.get_entry(cid)
    if entry and entry.get("status") == hst.ST_ADOPTED:
        return redirect(url_for("chapter", err="第 %d 章已采纳，标题/概要不可再修改" % no))
    title = (request.form.get("title") or "").strip()
    if not title:
        return redirect(url_for("chapter", err="标题不能为空"))
    summary = (request.form.get("summary") or "").strip()
    ost.update_chapter(cid, title=title, summary=summary)
    return redirect(url_for("chapter", msg="已更新第 %d 章的标题/概要" % no))


@app.route("/chapter/<cid>/meta_llm", methods=["POST"])
def chapter_meta_llm(cid):
    """LLM 依据章节正文（+ 可选用户要求）生成标题/概要，先给确认页，不直接替换。"""
    vol, ch, no = ost.find_chapter(cid)
    if not ch:
        return redirect(url_for("chapter", err="章节不存在"))
    entry = hst.get_entry(cid)
    if entry and entry.get("status") == hst.ST_ADOPTED:
        return redirect(url_for("chapter", err="第 %d 章已采纳，标题/概要不可再修改" % no))
    if not entry or not (entry.get("content") or "").strip():
        return redirect(url_for("chapter", err="第 %d 章还没有正文，无法让 LLM 总结" % no))
    requirement = (request.form.get("requirement") or "").strip()
    try:
        meta = cli.gen_chapter_meta(entry["content"], ch.get("title", ""),
                                    ch.get("summary", ""), requirement)
    except cli.LLMError as e:
        return redirect(url_for("chapter", err="LLM 生成标题/概要失败：" + str(e)))
    return render_template("chapter_meta_confirm.html", cid=cid, no=no,
                           old_title=ch.get("title", ""), old_summary=ch.get("summary", ""),
                           new_title=meta["title"], new_summary=meta["summary"],
                           requirement=requirement)


@app.route("/chapter/anchor/<cid>/save", methods=["POST"])
def chapter_anchor_save(cid):
    """手动修正章节场景锚点；三项全空等于清空锚点。"""
    vol, ch, no = ost.find_chapter(cid)
    if not ch:
        return redirect(url_for("chapter", err="章节不存在"))
    if not hst.get_entry(cid):
        return redirect(url_for("chapter", err="第 %d 章还没有生成记录，无需设置锚点" % no))
    time_v = (request.form.get("time") or "").strip()
    place = (request.form.get("place") or "").strip()
    present_raw = (request.form.get("present") or "").strip()
    for sep in ("、", "；", ";"):
        present_raw = present_raw.replace(sep, ",")
    present = [p.strip() for p in present_raw.split(",") if p.strip()]
    scene = {"time": time_v, "place": place, "present": present}
    hst.update_scene_end(cid, scene if (time_v or place or present) else None)
    return redirect(url_for("chapter", msg="已更新第 %d 章的场景锚点" % no
                            if (time_v or place or present) else "已清空第 %d 章的场景锚点" % no))


@app.route("/chapter/anchor/<cid>/extract", methods=["POST"])
def chapter_anchor_extract(cid):
    """对有正文的章节补抽场景锚点（旧章节没有锚点时人工触发，单次 LLM 调用）。"""
    vol, ch, no = ost.find_chapter(cid)
    if not ch:
        return redirect(url_for("chapter", err="章节不存在"))
    entry = hst.get_entry(cid)
    if not entry or not (entry.get("content") or "").strip():
        return redirect(url_for("chapter", err="第 %d 章还没有正文，无法抽取锚点" % no))
    if not lc.llm_available():
        return redirect(url_for("chapter", err="请先到「模型配置」页填写模型"))
    try:
        scene = cli.extract_scene_end(no, ch.get("title", ""), entry["content"])
    except cli.LLMError as e:
        return redirect(url_for("chapter", err="LLM 抽取锚点失败：" + str(e)))
    hst.update_scene_end(cid, scene)
    return redirect(url_for("chapter", msg="已抽取第 %d 章的场景锚点：%s｜%s"
                            % (no, scene.get("time") or "（时间未标注）",
                               scene.get("place") or "（地点未标注）")))


@app.route("/chapter/regenerate", methods=["POST"])
def chapter_regenerate():
    """重新生成所选章节（排队/失败/草稿），支持附加要求；也覆盖大纲里原有的待生成章。"""
    ids = _sorted_ids(request.form.getlist("chapter_ids"))
    if not ids:
        return redirect(url_for("chapter", err="请先勾选要重新生成的章节"))
    note = request.form.get("note", "").strip()
    # 先只读算出可重生成的章节：reset_for_regen 会清空正文，若先写盘再占槽，
    # 并发下占槽失败会把草稿清成 planned 孤儿（与 chapter_confirm 同一顺序：先 reserve 再写盘再 launch）
    to_regen, skipped = [], 0
    for cid in ids:
        entry = hst.get_entry(cid)
        if entry:
            if entry.get("status") in hst.REGENERABLE:
                to_regen.append((cid, "reset"))
            else:
                skipped += 1
        else:
            _, ch, _ = ost.find_chapter(cid)
            if ch and ch.get("status") == ost.STATUS_TODO:
                to_regen.append((cid, "new"))
            else:
                skipped += 1
    if not to_regen:
        return redirect(url_for("chapter", err="所选章节都不可重新生成（已采纳/已生成的章节不可推翻）"))
    tid = we.reserve_task("generate", len(to_regen))
    if tid is None:
        return redirect(url_for("chapter", err="已有任务在运行，请等它结束"))
    try:
        for cid, op in to_regen:
            if op == "reset":
                hst.reset_for_regen(cid, note)
            else:
                hst.upsert_entry(hst.new_entry(cid, min_words=we.DEFAULT_MIN_WORDS,
                                               max_words=we.DEFAULT_MAX_WORDS))
    except Exception as e:
        we.abort_task(tid, "重置章节失败：%s" % e)
        return redirect(url_for("chapter", err="重置章节失败：" + str(e)))
    we.launch_task(tid, "generate", [cid for cid, _ in to_regen])
    msg = "已加入重新生成队列：%d 章" % len(to_regen)
    if skipped:
        msg += "，跳过 %d 章（已采纳或已生成）" % skipped
    return redirect(url_for("chapter", task=tid, msg=msg))


@app.route("/chapter/adopt", methods=["POST"])
def chapter_adopt():
    """采纳所选草稿：后台逐章抽取事实，回写逐章摘要 / 角色状态 / 伏笔 / 大纲。"""
    ids = _sorted_ids(request.form.getlist("chapter_ids"))
    drafts = [cid for cid in ids
              if (hst.get_entry(cid) or {}).get("status") == hst.ST_DRAFT
              and hst.get_entry(cid).get("content")]
    if not drafts:
        return redirect(url_for("chapter", err="请勾选状态为「草稿」且有正文的章节"))
    if we.active_task():
        return redirect(url_for("chapter", err="已有任务在运行，请等它结束"))
    tid = we.start_adopt(drafts)
    if not tid:
        return redirect(url_for("chapter", err="已有任务在运行，请等它结束"))
    skipped = len(ids) - len(drafts)
    msg = "采纳任务已启动：%d 章" % len(drafts)
    if skipped:
        msg += "，跳过 %d 章（非草稿）" % skipped
    return redirect(url_for("chapter", task=tid, msg=msg))


@app.route("/chapter/task/<tid>")
def chapter_task(tid):
    """任务进度轮询。"""
    t = we.get_task(tid)
    if not t:
        return jsonify(ok=False), 404
    return jsonify(ok=True, task=t)


@app.route("/chapter/text/<cid>")
def chapter_text(cid):
    """章节正文阅读页（含审校记录）。"""
    entry = hst.get_entry(cid)
    vol, ch, no = ost.find_chapter(cid)
    if not entry or not entry.get("content") or not ch:
        return redirect(url_for("chapter", err="该章节还没有正文"))
    # 有正文的相邻章节（阅读顺序）
    entries = hst.all_entries()
    ordered = []
    for v, c, n in ost.iter_chapters():
        e = entries.get(c["id"])
        if e and e.get("content"):
            ordered.append((c["id"], n))
    prev_cid = next_cid = None
    for i, (c_id, n) in enumerate(ordered):
        if c_id == cid:
            prev_cid = ordered[i - 1][0] if i > 0 else None
            next_cid = ordered[i + 1][0] if i + 1 < len(ordered) else None
            break
    paragraphs = [p.strip() for p in entry["content"].split("\n") if p.strip()]
    return render_template("chapter_text.html", entry=entry, no=no, title=ch.get("title", ""),
                           vol=vol.get("title", "") if vol else "",
                           status_label=hst.STATUS_LABELS.get(entry.get("status"), ""),
                           review=(entry.get("review") or {}).get("final"),
                           history=(entry.get("review") or {}).get("history") or [],
                           paragraphs=paragraphs, prev_cid=prev_cid, next_cid=next_cid,
                           revise_draft=entry.get("revise") or None,
                           llm_on=lc.llm_available(),
                           msg=request.args.get("msg"), err=request.args.get("err"))


@app.route("/chapter/text/<cid>/save", methods=["POST"])
def chapter_text_save(cid):
    """保存用户手动编辑的正文：以用户文本为准，同步重算字数。"""
    entry = hst.get_entry(cid)
    if not entry:
        return redirect(url_for("chapter", err="章节不存在"))
    content = (request.form.get("content") or "").strip()
    if not content:
        return redirect(url_for("chapter_text", cid=cid, err="正文不能为空"))
    hst.update_content(cid, content, cli.count_words(content))
    return redirect(url_for("chapter_text", cid=cid, msg="正文已保存"))


@app.route("/chapter/text/<cid>/revise", methods=["POST"])
def chapter_text_revise(cid):
    """LLM 微调：当前正文 + 用户修改要求 → 修改稿，暂存后跳对比页（不走审校）。"""
    entry = hst.get_entry(cid)
    vol, ch, no = ost.find_chapter(cid)
    if not entry or not entry.get("content") or not ch:
        return redirect(url_for("chapter", err="该章节还没有正文"))
    instruction = (request.form.get("instruction") or "").strip()
    if not instruction:
        return redirect(url_for("chapter_text", cid=cid, err="请先填写修改要求"))
    try:
        new_text = cli.revise_content(ch.get("title", ""), entry["content"], instruction)
    except cli.LLMError as e:
        return redirect(url_for("chapter_text", cid=cid, err="LLM 修改失败：" + str(e)))
    hst.set_revise_draft(cid, instruction, new_text)
    return redirect(url_for("chapter_text_compare", cid=cid))


def _inline_diff(old, new):
    """段内字符级 diff：相同部分原样，差异部分包 <mark>（old=红删除线 / new=绿）。
    输入先 escape，输出可直接 |safe 渲染。"""
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    o_parts, n_parts = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        o_seg, n_seg = str(escape(old[i1:i2])), str(escape(new[j1:j2]))
        if tag == "equal":
            o_parts.append(o_seg)
            n_parts.append(n_seg)
        else:
            if tag in ("delete", "replace"):
                o_parts.append(f'<mark class="diff-del">{o_seg}</mark>')
            if tag in ("insert", "replace"):
                n_parts.append(f'<mark class="diff-ins">{n_seg}</mark>')
    return "".join(o_parts), "".join(n_parts)


def _diff_rows(old_text, new_text):
    """段落级 diff：相同段落折叠成一行提示；差异段落左右对齐，等长 replace 块做段内字符高亮。"""
    old_lines = [p.strip() for p in old_text.split("\n") if p.strip()]
    new_lines = [p.strip() for p in new_text.split("\n") if p.strip()]
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    rows = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            rows.append({"kind": "same", "count": i2 - i1})
            continue
        o_ls, n_ls = old_lines[i1:i2], new_lines[j1:j2]
        if tag == "replace" and len(o_ls) == len(n_ls):
            for o, n in zip(o_ls, n_ls):
                o_h, n_h = _inline_diff(o, n)
                rows.append({"kind": "mod", "old": o_h, "new": n_h})
            continue
        for ln in o_ls:
            rows.append({"kind": "del", "old": str(escape(ln)), "new": ""})
        for ln in n_ls:
            rows.append({"kind": "ins", "old": "", "new": str(escape(ln))})
    return rows


@app.route("/chapter/text/<cid>/compare")
def chapter_text_compare(cid):
    """原文 / LLM 修改稿 差异对比（相同段落折叠），用户二选一，另一份删除。"""
    entry = hst.get_entry(cid)
    vol, ch, no = ost.find_chapter(cid)
    draft = (entry or {}).get("revise") or {}
    if not entry or not ch or not (draft.get("text") or "").strip():
        return redirect(url_for("chapter_text", cid=cid, err="没有待处理的修改稿"))
    return render_template("chapter_compare.html", entry=entry, no=no,
                           title=ch.get("title", ""), draft=draft,
                           rows=_diff_rows(entry["content"], draft["text"]),
                           new_words=cli.count_words(draft["text"]))


@app.route("/chapter/text/<cid>/revise/apply", methods=["POST"])
def chapter_text_revise_apply(cid):
    """对比页二选一：采用修改稿替换原文，或保留原文丢弃修改稿。"""
    entry = hst.get_entry(cid)
    if not entry:
        return redirect(url_for("chapter", err="章节不存在"))
    if request.form.get("decision") == "new":
        text = ((entry.get("revise") or {}).get("text") or "").strip()
        if not text:
            return redirect(url_for("chapter_text", cid=cid, err="修改稿已不存在"))
        hst.apply_revise_draft(cid, cli.count_words(text))
        return redirect(url_for("chapter_text", cid=cid,
                                msg="已采用 LLM 修改稿替换原文（未走审校）"))
    hst.clear_revise_draft(cid)
    return redirect(url_for("chapter_text", cid=cid, msg="已保留原文，修改稿已删除"))


# ---------- 审校循环模块 ----------

@app.route("/review")
def review():
    """真实审校记录：来自写作工作台每章的 Critic 检查结果。"""
    cmap = we.chapter_map()
    rows = []
    for cid, e in hst.all_entries().items():
        rv = e.get("review") or {}
        if not rv.get("final"):
            continue
        meta = cmap.get(cid)
        if not meta:
            continue
        vol, ch, no = meta
        rows.append({"cid": cid, "no": no, "vol": vol.get("title", ""),
                     "title": ch.get("title", ""), "status": e.get("status"),
                     "status_label": hst.STATUS_LABELS.get(e.get("status"), ""),
                     "rounds": rv.get("rounds", 0), "final": rv["final"],
                     "updated_at": e.get("updated_at", "")})
    rows.sort(key=lambda r: r["no"], reverse=True)
    total = len(rows)
    first_pass = sum(1 for cid, e in hst.all_entries().items()
                     if (e.get("review") or {}).get("history")
                     and e["review"]["history"][0].get("pass"))
    avg = round(sum(r["final"].get("score", 0) for r in rows) / total, 1) if total else 0
    failed = sum(1 for r in rows if not r["final"].get("pass")
                 and r["status"] == hst.ST_DRAFT)
    stats = {"total": total, "first_pass": first_pass, "avg": avg, "failed": failed}
    return render_template("review.html", rows=rows, stats=stats)


# ---------- 角色列表模块 ----------

@app.route("/characters")
def characters():
    chars = cst.load_chars()
    chars.sort(key=lambda c: 0 if c.get("role_type") == "主角" else 1)
    by_id = {c["id"]: c for c in chars}
    incoming = {}
    for c in chars:
        for r in c.get("relationships", []):
            t = by_id.get(r.get("target"))
            if t:
                incoming.setdefault(t["id"], []).append(
                    {"from_name": c["name"], "relation": r.get("relation", ""), "note": r.get("note", "")})
    return render_template("characters.html", characters=chars, by_id=by_id,
                           incoming=incoming, edges=cst.relationship_overview(chars),
                           has_protagonist=cst.has_protagonist(),
                           wizard_done=ps.wizard_done(), llm_on=lc.llm_available(),
                           msg=request.args.get("msg"), err=request.args.get("err"))


@app.route("/characters/generate_protagonist", methods=["POST"])
def generate_protagonist():
    proj = ps.load_project()
    try:
        data = cl.generate_protagonist(proj)
    except cl.LLMError as e:
        return redirect(url_for("characters", err=str(e)))
    chars = cst.load_chars()
    new_c = cl.build_character(data, "主角", "wizard", chars)
    existing = next((c for c in chars if c.get("role_type") == "主角"), None)
    if not existing:
        cst.touch_state(new_c, "从作品档案生成")
        if cst.name_taken(new_c["name"]):
            return redirect(url_for("characters",
                                    err="生成的主角名「%s」与现有角色重名，请重试或先调整现有角色" % new_c["name"]))
        cst.upsert(new_c)
        return redirect(url_for("characters", msg=f"已生成主角「{new_c['name']}」"))

    # 已有主角 → 锁内覆盖档案，以锁内最新记录为准保留
    # id / 创建时间 / 对话记录 / 状态历史 / 手动维护的关系（不随档案覆盖丢失）
    def _apply(char):
        new_c["id"] = char["id"]
        new_c["created_at"] = char["created_at"]
        for k in ("chat_history", "state_history", "relationships"):
            new_c[k] = char.get(k, [])
        cst.touch_state(new_c, "从作品档案生成")
        if cst.name_taken(new_c["name"], exclude_id=char["id"]):
            raise ValueError("生成的主角名「%s」与现有角色重名，请重试或先调整现有角色" % new_c["name"])
        char.clear()
        char.update(new_c)

    try:
        cst.update_character(existing["id"], _apply)
    except ValueError as e:
        return redirect(url_for("characters", err=str(e)))
    return redirect(url_for("characters", msg=f"已生成主角「{new_c['name']}」"))


@app.route("/characters/new")
def character_new():
    return render_template("character_chat.html", mode="create", sid="",
                           history=[], data={}, done=False,
                           llm_on=lc.llm_available())


@app.route("/characters/new/chat", methods=["POST"])
def character_new_chat():
    msg = request.form.get("message", "").strip()
    if not msg:
        return jsonify(ok=False, detail="消息为空"), 400
    sid = request.form.get("sid", "").strip()
    draft = cst.get_draft(sid) if sid else None
    if draft is None:
        draft = cst.create_draft()
    sid = draft["id"]
    try:
        result = cl.char_chat_turn(draft, msg)  # 只读快照做 LLM 计算，不修改草稿
    except cl.LLMError as e:
        return jsonify(ok=False, detail=str(e)), 502
    new_msgs = [{"role": "user", "content": msg},
                {"role": "assistant", "content": result["reply"]}]

    def _merge(d):
        d["history"].extend(new_msgs)
        cl.merge_extracted_data(d["data"], result["extracted"])
        d["done"] = result["done"]

    draft = cst.update_draft(sid, _merge)  # 锁内合并写回，不与并发请求互相覆盖
    if draft is None:
        return jsonify(ok=False, detail="对话草稿不存在"), 404
    if not result["done"]:
        return jsonify(ok=True, reply=result["reply"], done=False,
                       sid=sid, data=draft["data"])

    # 建档：锁内重检姓名唯一后，原子完成「建档 + 清草稿」
    char = cl.build_character(draft["data"], draft["data"].get("role_type") or "配角",
                              "dialog", cst.load_chars())
    char["chat_history"] = list(draft["history"])  # 创建对话留档，作为后续对话调整的上下文
    cst.touch_state(char, "对话创建")
    if cst.promote_draft(sid, char):
        return jsonify(ok=True, reply=result["reply"], done=True,
                       sid=sid, data=draft["data"], char_id=char["id"])
    # 同名不入库：打回对话让用户换个名字
    reply = "角色「%s」已经存在了，请给这个角色换一个名字。" % char["name"]

    def _rollback(d):
        d["done"] = False
        if d["history"] and d["history"][-1].get("role") == "assistant":
            d["history"][-1]["content"] = reply

    cst.update_draft(sid, _rollback)
    return jsonify(ok=True, reply=reply, done=False, sid=sid, data=draft["data"])


def _brief(chars):
    return [{"id": c["id"], "name": c["name"], "role_type": c["role_type"]} for c in chars]


@app.route("/characters/new/manual")
def character_new_manual():
    others = cst.load_chars()
    return render_template("character_edit.html", char=cst.new_character(),
                           others=others, others_brief=_brief(others),
                           state_keys=cst.STATE_KEYS, is_new=True)


@app.route("/characters/<cid>/edit")
def character_edit(cid):
    char = cst.get(cid)
    if not char:
        return redirect(url_for("characters", err="角色不存在"))
    others = [c for c in cst.load_chars() if c["id"] != cid]
    return render_template("character_edit.html", char=char, others=others,
                           others_brief=_brief(others),
                           state_keys=cst.STATE_KEYS, is_new=False)


def _char_form_data():
    """从表单解析角色字段（新建与编辑共用）。"""
    data = {
        "name": request.form.get("name", "").strip(),
        "role_type": request.form.get("role_type", "配角"),
        "gender": request.form.get("gender", "").strip(),
        "personality": request.form.get("personality", "").strip(),
        "background": request.form.get("background", "").strip(),
        "appearance": request.form.get("appearance", "").strip(),
        "state": {k: request.form.get(f"state__{k}", "").strip() for k in cst.STATE_KEYS},
        "relationships": [],
    }
    targets = request.form.getlist("rel_target")
    relations = request.form.getlist("rel_relation")
    notes = request.form.getlist("rel_note")
    for t, rel, note in zip(targets, relations, notes):
        if t:
            data["relationships"].append({"target": t, "relation": rel.strip(), "note": note.strip()})
    return data


def _fill_char(char, data, note):
    """把表单数据写进角色记录，并记录状态变更历史。"""
    state_changed = any(char["state"].get(k) != v for k, v in data["state"].items() if v or char["state"].get(k))
    for k in ("name", "role_type", "gender", "personality", "background", "appearance"):
        char[k] = data[k]
    char["state"] = data["state"]
    char["relationships"] = data["relationships"]
    if state_changed:
        cst.touch_state(char, note)


@app.route("/characters/create", methods=["POST"])
def character_create():
    char = cst.new_character()
    _fill_char(char, _char_form_data(), "手动创建")
    char["source"] = "manual"
    if not char["name"]:
        return redirect(url_for("characters", err="角色名不能为空"))
    if cst.name_taken(char["name"]):
        return redirect(url_for("characters", err="已存在同名角色「%s」，请换一个名字" % char["name"]))
    if not char["state_history"]:
        cst.touch_state(char, "手动创建")
    cst.upsert(char)
    return redirect(url_for("characters", msg=f"已创建角色「{char['name']}」"))


@app.route("/characters/<cid>/save", methods=["POST"])
def character_save(cid):
    data = _char_form_data()

    def _apply(char):
        _fill_char(char, data, "手动修改状态")
        if not char["name"]:
            raise ValueError("角色名不能为空")
        if cst.name_taken(char["name"], exclude_id=cid):
            raise ValueError("已存在同名角色「%s」，请换一个名字" % char["name"])

    try:
        char = cst.update_character(cid, _apply)
    except ValueError as e:
        return redirect(url_for("characters", err=str(e)))
    if not char:
        return redirect(url_for("characters", err="角色不存在"))
    return redirect(url_for("characters", msg=f"已保存「{char['name']}」"))


@app.route("/characters/<cid>/adjust")
def character_adjust(cid):
    char = cst.get(cid)
    if not char:
        return redirect(url_for("characters", err="角色不存在"))
    return render_template("character_chat.html", mode="adjust", char=char,
                           history=char.get("chat_history", []),
                           data={}, done=False, llm_on=lc.llm_available())


@app.route("/characters/<cid>/chat", methods=["POST"])
def character_chat(cid):
    msg = request.form.get("message", "").strip()
    if not msg:
        return jsonify(ok=False, detail="消息为空"), 400
    char = cst.get(cid)  # 无锁快照，仅作 LLM 上下文；写回走锁内 mutator
    if not char:
        return jsonify(ok=False, detail="角色不存在"), 404
    draft = {"history": list(char.get("chat_history", [])), "data": {}}
    try:
        result = cl.char_chat_turn(draft, msg, char=char)
    except cl.LLMError as e:
        return jsonify(ok=False, detail=str(e)), 502
    new_msgs = [{"role": "user", "content": msg},
                {"role": "assistant", "content": result["reply"]}]
    data = {}
    cl.merge_extracted_data(data, result["extracted"])  # 清洗抽取字段（纯函数）
    reply_extra = ""

    def _apply(c):
        nonlocal reply_extra
        c.setdefault("chat_history", []).extend(new_msgs)
        if data:
            state_before = dict(c["state"])
            name_before = c["name"]
            cl.apply_char_data(c, data, cst.load_chars())
            if c["name"] != name_before and cst.name_taken(c["name"], exclude_id=cid):
                c["name"] = name_before
                reply_extra = "（新名字与现有角色重复，未改名）"
            if c["state"] != state_before:
                cst.touch_state(c, "对话调整")

    char = cst.update_character(cid, _apply)
    if not char:
        return jsonify(ok=False, detail="角色不存在"), 404
    return jsonify(ok=True, reply=result["reply"] + reply_extra, done=False, char=char)


@app.route("/characters/<cid>/delete", methods=["POST"])
def character_delete(cid):
    char = cst.get(cid)
    if not char:
        return redirect(url_for("characters", err="角色不存在"))
    cst.delete(cid)
    return redirect(url_for("characters", msg=f"已删除角色「{char['name']}」"))


# ---------- 章节大纲模块 ----------

OUTLINE_PAGE_SIZE = 10  # 每卷章节列表的分页大小


@app.route("/outline")
def outline():
    store = ost.load_store()
    # 每卷独立分页：查询参数 p_<卷id>，翻页后锚点跳回本卷
    page_data = {}
    for vol in store["volumes"]:
        try:
            p = int(request.args.get("p_" + vol["id"], 1))
        except ValueError:
            p = 1
        chapters = vol.get("chapters", [])
        total = len(chapters)
        pages = max(1, (total + OUTLINE_PAGE_SIZE - 1) // OUTLINE_PAGE_SIZE)
        p = min(max(1, p), pages)
        start = (p - 1) * OUTLINE_PAGE_SIZE
        # (卷内序号, 章节) 序号跟随分页偏移连续编号；倒序展示，最新章在前
        rows_all = list(enumerate(chapters, 1))[::-1]
        page_data[vol["id"]] = {
            "page": p,
            "pages": pages,
            "total": total,
            "rows": rows_all[start:start + OUTLINE_PAGE_SIZE],
        }

    def page_url(vid, p):
        params = {"p_" + v["id"]: (p if v["id"] == vid else page_data[v["id"]]["page"])
                  for v in store["volumes"]}
        return url_for("outline", **params) + "#vol-" + vid

    return render_template("outline.html", store=store, page_data=page_data,
                           page_url=page_url,
                           msg=request.args.get("msg"), err=request.args.get("err"))


@app.route("/outline/main", methods=["POST"])
def outline_main():
    ost.set_main(request.form.get("main", "").strip())
    return redirect(url_for("outline", msg="全书主线已更新"))


@app.route("/outline/volume/add", methods=["POST"])
def outline_volume_add():
    title = request.form.get("title", "").strip()
    if not title:
        return redirect(url_for("outline", err="卷标题不能为空"))
    vol = ost.add_volume(title, request.form.get("summary", "").strip())
    return redirect(url_for("outline", msg=f"已创建卷「{vol['title']}」") + "#vol-" + vol["id"])


@app.route("/outline/volume/<vid>/edit", methods=["POST"])
def outline_volume_edit(vid):
    title = request.form.get("title", "").strip()
    if not title:
        return redirect(url_for("outline", err="卷标题不能为空"))
    vol = ost.update_volume(vid, title, request.form.get("summary", "").strip())
    if not vol:
        return redirect(url_for("outline", err="卷不存在"))
    return redirect(url_for("outline", msg="卷信息已更新") + "#vol-" + vid)


@app.route("/outline/volume/<vid>/delete", methods=["POST"])
def outline_volume_delete(vid):
    vol = ost.get_volume(vid)
    if not vol:
        return redirect(url_for("outline", err="卷不存在"))
    ost.delete_volume(vid)
    return redirect(url_for("outline", msg=f"已删除卷「{vol['title']}」（含 {len(vol.get('chapters', []))} 章）"))


@app.route("/outline/chapter/<cid>/delete", methods=["POST"])
def outline_chapter_delete(cid):
    ch = ost.delete_chapter(cid)
    if not ch:
        return redirect(url_for("outline", err="章节不存在"))
    return redirect(url_for("outline", msg=f"已删除章节「{ch['title']}」"))


# ---------- 分层记忆模块 ----------

MEMORY_PER_PAGE = 10


def _page_arg(name):
    try:
        return max(1, int(request.args.get(name, 1)))
    except (TypeError, ValueError):
        return 1


@app.route("/memory")
def memory():
    store = mst.load_store()
    # 倒序：最新章节/最新范围排在最前
    chapters = sorted(store["chapter_summaries"], key=lambda c: c.get("no", 0), reverse=True)
    merged = sorted(store["merged_summaries"],
                    key=lambda m: (m.get("from_no") or 0, m.get("to_no") or 0), reverse=True)
    cp, mp = _page_arg("cp"), _page_arg("mp")
    c_pages = max(1, -(-len(chapters) // MEMORY_PER_PAGE))
    m_pages = max(1, -(-len(merged) // MEMORY_PER_PAGE))
    cp, mp = min(cp, c_pages), min(mp, m_pages)
    meta = [{"id": c["chapter_id"], "no": c.get("no", 0), "vol": c.get("vol") or ""}
            for c in chapters]
    covered = mst.covered_nos()
    # 近章原文：最近两章已有正文的章节（写作工作台生成/采纳后自动出现在这里）
    entries = hst.all_entries()
    recent_texts = []
    for v, ch, no in ost.iter_chapters():
        e = entries.get(ch["id"])
        if e and e.get("content"):
            recent_texts.append({"no": no, "title": ch.get("title", ""), "cid": ch["id"],
                                 "excerpt": e["content"][:260]})
    recent_texts = recent_texts[-2:][::-1]
    return render_template(
        "memory.html",
        chapters=chapters[(cp - 1) * MEMORY_PER_PAGE:cp * MEMORY_PER_PAGE],
        merged=merged[(mp - 1) * MEMORY_PER_PAGE:mp * MEMORY_PER_PAGE],
        cp=cp, c_pages=c_pages, mp=mp, m_pages=m_pages,
        c_total=len(chapters), m_total=len(merged),
        chapters_meta=meta, covered=covered, covered_list=sorted(covered),
        recent_texts=recent_texts,
        llm_on=lc.llm_available(),
        msg=request.args.get("msg"), err=request.args.get("err"))


@app.route("/memory/sync", methods=["POST"])
def memory_sync():
    """从章节大纲同步逐章摘要（不调 LLM）。不覆盖正文抽取的摘要，并清理大纲中已删除的章节。"""
    existing = {c.get("chapter_id"): c for c in mst.load_store()["chapter_summaries"]}
    seen, synced, skipped, no = [], 0, 0, 0
    for v in ost.load_store()["volumes"]:
        for ch in v.get("chapters", []):
            no += 1
            seen.append(ch["id"])
            if not ch.get("summary"):
                continue
            cur = existing.get(ch["id"])
            if cur and cur.get("source") == mst.SOURCE_TEXT:
                skipped += 1
                continue
            mst.upsert_chapter_summary(ch["id"], no, v.get("title", ""),
                                       ch.get("title", ""), ch["summary"], mst.SOURCE_OUTLINE)
            synced += 1
    removed = mst.remove_stale(seen)
    msg = f"已同步 {synced} 条章节摘要"
    if skipped:
        msg += f"，跳过 {skipped} 条正文摘要"
    if removed:
        msg += f"，清理 {removed} 条已删章节"
    return redirect(url_for("memory", msg=msg))


COMPRESS_WINDOW = 10  # 每次 LLM 调用压缩的最大章数


@app.route("/memory/compress", methods=["POST"])
def memory_compress():
    """选区压缩：勾选的章节按「连续且同卷」拆段，段内每 ≤10 章一次 LLM 调用。

    逐窗落盘（删重叠旧段与追加新段是一次原子写），部分失败保留已生成的段。
    """
    ids = request.form.getlist("chapter_ids")
    if not ids:
        return redirect(url_for("memory", err="请先勾选要压缩的章节摘要"))
    ids = list(dict.fromkeys(ids))  # 去重（前端跨页注入与可见复选框可能重复提交）
    by_id = {c["chapter_id"]: c for c in mst.load_store()["chapter_summaries"]}
    chosen = sorted((by_id[i] for i in ids if i in by_id), key=lambda c: c.get("no", 0))
    if not chosen:
        return redirect(url_for("memory", err="勾选的章节不存在，请先「从大纲同步」"))
    runs, run = [], [chosen[0]]
    for c in chosen[1:]:
        prev = run[-1]
        if c.get("no") == prev.get("no", 0) + 1 and c.get("vol") == prev.get("vol"):
            run.append(c)
        else:
            runs.append(run)
            run = [c]
    runs.append(run)
    windows = [r[i:i + COMPRESS_WINDOW] for r in runs for i in range(0, len(r), COMPRESS_WINDOW)]
    made, failed = 0, []
    for w in windows:
        a, b = w[0]["no"], w[-1]["no"]
        label = f"第{a}章" if a == b else f"第{a}-{b}章"
        if w[0].get("vol"):
            label += f" · {w[0]['vol']}"
        try:
            summary = cli.compress_window(w)
        except cli.LLMError as e:
            failed.append(f"{label}（{e}）")
            continue
        mst.replace_overlapping(a, b, [{"from_no": a, "to_no": b, "range": label, "summary": summary}])
        made += 1
    if not made and failed:
        return redirect(url_for("memory", err="压缩失败：" + "；".join(failed)))
    msg = f"已生成 {made} 段合并摘要（覆盖 {len(chosen)} 章）"
    if failed:
        msg += f"，{len(failed)} 段失败：" + "；".join(failed)
    return redirect(url_for("memory", msg=msg))


@app.route("/memory/merged/<mid>/delete", methods=["POST"])
def memory_merged_delete(mid):
    if not mst.delete_merged(mid):
        return redirect(url_for("memory", err="合并摘要不存在"))
    return redirect(url_for("memory", msg="已删除合并摘要"))


# ---------- 世界观设定库模块 ----------

@app.route("/bible")
def bible():
    groups = bst.grouped()
    return render_template("bible.html", groups=groups,
                           categories=[c for c, _ in groups],
                           llm_on=lc.llm_available(), wizard_done=ps.wizard_done(),
                           msg=request.args.get("msg"), err=request.args.get("err"))


def _add_entries(entries):
    """批量入库设定条目，同分类同名去重。返回 (添加数, 跳过数)。"""
    existing = bst.existing_names()
    added = 0
    for e in entries:
        key = (e["category"], e["name"])
        if key in existing:
            continue
        bst.add_entry(e["category"], e["name"], e["content"])
        existing.add(key)
        added += 1
    return added, len(entries) - added


@app.route("/bible/init", methods=["POST"])
def bible_init():
    try:
        entries = cli.init_bible(ps.load_project())
    except cli.LLMError as e:
        return redirect(url_for("bible", err=str(e)))
    added, skipped = _add_entries(entries)
    msg = f"已生成 {added} 条初始设定"
    if skipped:
        msg += f"（{skipped} 条同名已跳过）"
    return redirect(url_for("bible", msg=msg))


@app.route("/bible/extend", methods=["POST"])
def bible_extend():
    category = request.form.get("category", "").strip()
    if not category:
        return redirect(url_for("bible", err="缺少分类"))
    names = [e["name"] for e in bst.load_store()["entries"] if e.get("category") == category]
    try:
        entries = cli.extend_bible_category(ps.load_project(), category, names,
                                            request.form.get("hint", "").strip())
    except cli.LLMError as e:
        return redirect(url_for("bible", err=str(e)))
    added, _ = _add_entries(entries)
    return redirect(url_for("bible", msg=f"「{category}」新增 {added} 条设定"))


@app.route("/bible/add", methods=["POST"])
def bible_add():
    category = request.form.get("category", "").strip()
    name = request.form.get("name", "").strip()
    content = request.form.get("content", "").strip()
    if not (category and name and content):
        return redirect(url_for("bible", err="分类、条目名、内容均需填写"))
    bst.add_entry(category, name, content)
    return redirect(url_for("bible", msg=f"已添加设定「{name}」"))


@app.route("/bible/entry/<eid>/edit", methods=["POST"])
def bible_entry_edit(eid):
    category = request.form.get("category", "").strip()
    name = request.form.get("name", "").strip()
    content = request.form.get("content", "").strip()
    if not (category and name and content):
        return redirect(url_for("bible", err="分类、条目名、内容均需填写"))
    if not bst.update_entry(eid, category, name, content):
        return redirect(url_for("bible", err="条目不存在"))
    return redirect(url_for("bible", msg=f"已保存「{name}」"))


@app.route("/bible/entry/<eid>/delete", methods=["POST"])
def bible_entry_delete(eid):
    if not bst.delete_entry(eid):
        return redirect(url_for("bible", err="条目不存在"))
    return redirect(url_for("bible", msg="已删除设定条目"))


# ---------- 伏笔追踪模块 ----------

FORESHADOW_PER_PAGE = 10


def _fid_no(item):
    """F-001 → 1，用于编号排序。"""
    digits = "".join(ch for ch in str(item.get("id", "")) if ch.isdigit())
    return int(digits) if digits else 0


@app.route("/foreshadow")
def foreshadow():
    _, _, done_ch = _outline_stats()
    due = fst.due_items(done_ch)
    # 编号倒序：最新的伏笔排在最前
    items = sorted(fst.load_store()["items"], key=_fid_no, reverse=True)
    status_filter = request.args.get("status", "").strip()
    if status_filter in fst.STATUSES:
        items = [it for it in items if it.get("status") == status_filter]
    else:
        status_filter = ""
    page = min(_page_arg("page"), max(1, -(-len(items) // FORESHADOW_PER_PAGE)))
    pages = max(1, -(-len(items) // FORESHADOW_PER_PAGE))
    return render_template("foreshadow.html",
                           items=items[(page - 1) * FORESHADOW_PER_PAGE:page * FORESHADOW_PER_PAGE],
                           page=page, pages=pages, total=len(items),
                           status_filter=status_filter,
                           due_ids={it["id"] for it in due}, done_ch=done_ch,
                           statuses=fst.STATUSES,
                           llm_on=lc.llm_available(), wizard_done=ps.wizard_done(),
                           msg=request.args.get("msg"), err=request.args.get("err"))


@app.route("/foreshadow/brainstorm", methods=["POST"])
def foreshadow_brainstorm():
    try:
        items = cli.brainstorm_foreshadows(ps.load_project(), ost.load_store())
    except cli.LLMError as e:
        return redirect(url_for("foreshadow", err=str(e)))
    for it in items:
        fst.add_item(it["content"], it["planted"], it["plan_recycle"], it["status"])
    return redirect(url_for("foreshadow", msg=f"已生成 {len(items)} 条伏笔，可继续编辑或删除"))


def _foreshadow_back(**extra):
    """增删改后回跳列表，保留当前状态筛选与页码（表单用隐藏域 k_status/k_page 带回）。"""
    keep = {}
    if request.form.get("k_status", "") in fst.STATUSES:
        keep["status"] = request.form["k_status"]
    try:
        keep["page"] = max(1, int(request.form.get("k_page", 1)))
    except (TypeError, ValueError):
        pass
    return redirect(url_for("foreshadow", **keep, **extra))


@app.route("/foreshadow/add", methods=["POST"])
def foreshadow_add():
    content = request.form.get("content", "").strip()
    if not content:
        return _foreshadow_back(err="伏笔内容不能为空")
    fst.add_item(content, request.form.get("planted", "").strip(),
                 request.form.get("plan_recycle", "").strip(),
                 request.form.get("status", "待回收"))
    return _foreshadow_back(msg="已添加伏笔")


@app.route("/foreshadow/<fid>/save", methods=["POST"])
def foreshadow_save(fid):
    content = request.form.get("content", "").strip()
    if not content:
        return _foreshadow_back(err="伏笔内容不能为空")
    if not fst.update_item(fid, content, request.form.get("planted", "").strip(),
                           request.form.get("plan_recycle", "").strip(),
                           request.form.get("status", "待回收")):
        return _foreshadow_back(err="伏笔不存在")
    return _foreshadow_back(msg=f"已保存 {fid}")


@app.route("/foreshadow/<fid>/delete", methods=["POST"])
def foreshadow_delete(fid):
    if not fst.delete_item(fid):
        return _foreshadow_back(err="伏笔不存在")
    return _foreshadow_back(msg=f"已删除 {fid}")


@app.route("/foreshadow/batch_delete", methods=["POST"])
def foreshadow_batch_delete():
    """批量删除勾选的伏笔（复选框通过 form 属性挂到表格外部的批量表单）。"""
    fids = list(dict.fromkeys(request.form.getlist("fids")))
    if not fids:
        return _foreshadow_back(err="请先勾选要删除的伏笔")
    deleted = sum(1 for fid in fids if fst.delete_item(fid))
    return _foreshadow_back(msg=f"已删除 {deleted} 条伏笔")


# ---------- 文风控制模块 ----------

@app.route("/style")
def style():
    return render_template("style.html", store=sst.load_store(), keys=sst.CONSTRAINT_KEYS,
                           style_prompt=ps.load_project().get("style_prompt", ""),
                           llm_on=lc.llm_available(),
                           msg=request.args.get("msg"), err=request.args.get("err"))


@app.route("/style/extract", methods=["POST"])
def style_extract():
    sp = ps.load_project().get("style_prompt", "").strip()
    if not sp:
        return redirect(url_for("style", err="作品档案还没有文风规范，请先完成新书向导"))
    try:
        constraints = cli.extract_style_constraints(sp)
    except cli.LLMError as e:
        return redirect(url_for("style", err=str(e)))
    sst.save_constraints(constraints)
    return redirect(url_for("style", msg="已从文风规范提取结构化约束"))


@app.route("/style/save", methods=["POST"])
def style_save():
    sst.save_constraints({k: request.form.get(k, "") for k in sst.CONSTRAINT_KEYS})
    return redirect(url_for("style", msg="风格约束已保存"))


@app.route("/style/sample/generate", methods=["POST"])
def style_sample_generate():
    proj = ps.load_project()
    sp = proj.get("style_prompt", "").strip()
    if not sp:
        return redirect(url_for("style", err="作品档案还没有文风规范，请先完成新书向导"))
    try:
        sample = cli.generate_style_sample(proj, sp)
    except cli.LLMError as e:
        return redirect(url_for("style", err=str(e)))
    sst.add_sample(sample, "llm")
    return redirect(url_for("style", msg="已生成一段样章"))


@app.route("/style/sample/add", methods=["POST"])
def style_sample_add():
    content = request.form.get("content", "").strip()
    if not content:
        return redirect(url_for("style", err="样章内容不能为空"))
    sst.add_sample(content, "manual")
    return redirect(url_for("style", msg="已添加样章"))


@app.route("/style/sample/<sid>/delete", methods=["POST"])
def style_sample_delete(sid):
    if not sst.delete_sample(sid):
        return redirect(url_for("style", err="样章不存在"))
    return redirect(url_for("style", msg="已删除样章"))


def _test_connection(slot_cfg):
    """发一条 max_tokens=1 的 chat 请求验证配置可用，返回 (ok, detail)。按 protocol 分发。"""
    base_url = (slot_cfg.get("base_url") or "").rstrip("/")
    api_key = slot_cfg.get("api_key") or ""
    model = slot_cfg.get("model") or ""
    if not (base_url and api_key and model):
        return False, "配置不完整：base_url / api_key / model 均需填写"
    if slot_cfg.get("protocol") == "anthropic":
        payload = json.dumps({
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode("utf-8")
        req = urllib.request.Request(
            lc._anthropic_url(base_url),
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
    else:
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }).encode("utf-8")
        req = urllib.request.Request(
            base_url + "/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
            method="POST",
        )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            is_sse = "text/event-stream" in (resp.headers.get("Content-Type") or "")
        if is_sse or raw.lstrip().startswith("event:"):
            return True, f"连接成功，模型以 SSE 流式响应（{model}）"
        body = json.loads(raw)
        got = body.get("model") or model
        return True, f"连接成功，模型响应正常（{got}）"
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8"))
            msg = err.get("error", {}).get("message") or str(err)
        except Exception:
            msg = e.reason
        return False, f"HTTP {e.code}：{msg}"
    except Exception as e:
        return False, f"请求失败：{e}"


def _list_models(base_url, api_key, protocol="openai"):
    """GET 模型列表接口拉取该 key 可用的模型，返回 (ok, ids 或错误信息)。按 protocol 分发。"""
    if not (base_url and api_key):
        return False, "需要 base_url 和 api_key 才能拉取模型列表"
    if protocol == "anthropic":
        base = base_url.rstrip("/")
        url = base + "/models" if base.endswith("/v1") else base + "/v1/models"
        req = urllib.request.Request(
            url,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        )
    else:
        req = urllib.request.Request(
            base_url.rstrip("/") + "/models",
            headers={"Authorization": "Bearer " + api_key},
        )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return False, "模型列表接口返回了非 JSON 内容（该转发端点可能未实现 /models，请手填模型名）"
        ids = sorted(m["id"] for m in body.get("data", []) if m.get("id"))
        if not ids:
            return False, "接口返回成功但模型列表为空"
        return True, ids
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8"))
            msg = err.get("error", {}).get("message") or str(err)
        except Exception:
            msg = e.reason
        return False, f"HTTP {e.code}：{msg}"
    except Exception as e:
        return False, f"请求失败：{e}"


def _form_or_saved(cfg, slot):
    """优先用表单里填的值（未保存也可测试/拉取），留空则回退到已保存的生效配置。"""
    eff = cs.effective_slot(cfg, slot)
    return {
        "base_url": request.form.get("base_url", "").strip() or eff.get("base_url", ""),
        "api_key": request.form.get("api_key", "").strip() or eff.get("api_key", ""),
        "model": request.form.get("model", "").strip() or eff.get("model", ""),
        "protocol": request.form.get("protocol", "").strip() or eff.get("protocol", "openai"),
    }


def _render_settings(msg=None):
    cfg = cs.load_config()
    effective = {slot: cs.effective_slot(cfg, slot) for slot in cs.SLOTS}
    return render_template("settings_models.html", slots=cs.SLOTS, presets=cs.PRESETS,
                           cfg=cfg["slots"], effective=effective, mask=cs.mask_key,
                           msg=msg)


@app.route("/settings/models")
def settings_models():
    return _render_settings(msg=request.args.get("msg"))


@app.route("/settings/models/save", methods=["POST"])
def settings_models_save():
    """单表单统一保存全部槽位，字段名形如 generation__base_url。"""
    for slot in cs.SLOTS:
        data = {
            "preset": request.form.get(f"{slot}__preset", "custom"),
            "base_url": request.form.get(f"{slot}__base_url", "").strip(),
            "api_key": request.form.get(f"{slot}__api_key", "").strip(),
            "model": request.form.get(f"{slot}__model", "").strip(),
            "protocol": request.form.get(f"{slot}__protocol", "openai"),
            "inherit": request.form.get(f"{slot}__inherit") == "1",
        }
        cs.save_slot(slot, data)
    return redirect(url_for("settings_models", msg="全部配置已保存"))


@app.route("/settings/models/test", methods=["POST"])
def settings_models_test():
    slot = request.form.get("slot", "")
    if slot not in cs.SLOTS:
        return jsonify(ok=False, detail="未知槽位"), 400
    cfg = cs.load_config()
    ok, detail = _test_connection(_form_or_saved(cfg, slot))
    return jsonify(ok=ok, detail=detail)


@app.route("/settings/models/fetch", methods=["POST"])
def settings_models_fetch():
    slot = request.form.get("slot", "")
    if slot not in cs.SLOTS:
        return jsonify(ok=False, detail="未知槽位"), 400
    cfg = cs.load_config()
    form = _form_or_saved(cfg, slot)
    ok, result = _list_models(form["base_url"], form["api_key"], form["protocol"])
    if ok:
        return jsonify(ok=True, models=result, detail=f"拉到 {len(result)} 个可用模型")
    return jsonify(ok=False, detail=result)


# ---------- 新书向导（首次写作引导） ----------

@app.route("/setup")
def setup():
    # 首次进入时给当前步骤补一条开场白（锁内判空+追加+落盘，并发幂等）
    proj = ps.ensure_wizard_opener(lc.STEP_OPENERS)
    cur = proj["wizard"]["step"]
    ids = [s[0] for s in lc.STEPS]
    cur_idx = ids.index(cur) if cur in ids else len(ids)  # "done" 视为全部完成
    return render_template("setup.html", project=proj, steps=lc.STEPS,
                           cur_idx=cur_idx, llm_on=lc.llm_available())


@app.route("/setup/chat", methods=["POST"])
def setup_chat():
    msg = request.form.get("message", "").strip()
    if not msg:
        return jsonify(ok=False, detail="消息为空"), 400
    proj = ps.load_project()  # 锁外快照，仅作 LLM 上下文；写回走锁内合并
    try:
        result = lc.wizard_turn(proj, msg)
    except lc.LLMError as e:
        return jsonify(ok=False, detail=str(e)), 502
    box = {}

    def _merge(p):
        box["reply"] = lc.apply_wizard_turn(p, msg, result)

    proj = ps.update_project(_merge)  # 锁内合并写回，不与并发对话互相覆盖
    step = proj["wizard"]["step"]
    return jsonify(ok=True, reply=box["reply"], step=step,
                   done=step == "done", project=proj)


@app.route("/setup/save", methods=["POST"])
def setup_save():
    fields = {k: request.form.get(k, "").strip() for k in
              ("title", "genre", "audience", "length", "logline", "synopsis", "style_prompt")}
    background = {k: request.form.get(k, "").strip() for k in ("era", "rules", "stage")}
    ps.save_setup_fields(fields, background)  # 锁内合并
    return redirect(url_for("dashboard"))


@app.route("/setup/reset", methods=["POST"])
def setup_reset():
    ps.reset_wizard()
    return redirect(url_for("setup"))


# ---------- AI 聊天助手（全局悬浮面板） ----------


@app.route("/assistant/history")
def assistant_history():
    return jsonify(ok=True, messages=kst.load_history())


@app.route("/assistant/clear", methods=["POST"])
def assistant_clear():
    kst.clear_history()
    return jsonify(ok=True)


@app.route("/assistant/chat", methods=["POST"])
def assistant_chat():
    """流式聊天：text/plain 逐段输出；流结束后把整轮对话落盘。"""
    msg = request.form.get("message", "").strip()
    if not msg:
        return jsonify(ok=False, detail="消息为空"), 400

    def gen():
        if not lc.slot_available("chat"):
            yield "❌ 未配置聊天模型，请先到「模型配置」页配置「AI 聊天助手」槽位。"
            return
        sys_prompt = prm.ASSISTANT_SYS + prm.p_assistant_context(ps.load_project())
        history = [{"role": m["role"], "content": m["content"]}
                   for m in kst.load_history(tail=kst.CONTEXT_TAIL)]
        messages = [{"role": "system", "content": sys_prompt}] + history + [
            {"role": "user", "content": msg}]
        parts = []
        try:
            for delta in lc.chat_stream(messages, slot="chat"):
                parts.append(delta)
                yield delta
        except lc.LLMError as e:
            yield "\n\n❌ %s" % e
            return
        reply = "".join(parts).strip()
        if reply:
            kst.append_turn(msg, reply)  # 锁内追加，不与并发对话互相覆盖

    return Response(stream_with_context(gen()), mimetype="text/plain; charset=utf-8")


if __name__ == "__main__":
    import os

    # 5000 被 macOS 隔空播放接收器(ControlCenter)占用，默认用 5001，可用 WRITER_PORT 覆盖
    port = int(os.environ.get("WRITER_PORT", "5001"))
    # debug 关闭：无自动重载，改代码后需重启进程生效
    app.run(debug=False, port=port)
