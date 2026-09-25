"""隙光 GapLight —— 课隙计算与编排引擎（v2）。

分层结构：
1. 硬约束（一票否决）：时间在未来、课隙 ≥15 分钟、扣 10 分钟过渡、
   步骤 ≤ 可用时间、当日学习任务总占用 ≤ 留白上限、不动锁定项；
2. 四因素打分：紧急度 40% / 时长匹配 25% / 精力匹配 20% / 负担与切换 15%
   （权重为可调参数，见设置页）。

v2 新增：
- 课程分单双周（以"学期第一周周一"为基准过滤）；
- 三餐与午休作为每日固定安排，自然切出课隙；
- 番茄节奏：学习块间插入 5 分钟休息，每 4 个番茄后 15 分钟大休息；
- 校园跑：第 2~16 周累计 ≥30 次，每周 2 次（落后进度 3 次），
  安排在当天最后一项，跑完洗澡休息。

编排原则：无合适任务宁可留白并说明原因；临近截止但课隙不足时提示风险。
"""
from __future__ import annotations

from datetime import date as dt_date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .models import (Course, Event, Task, Step, PlanItem,
                     STEP_PENDING, STEP_MISSED, ITEM_PLANNED,
                     KIND_TASK, KIND_REST, KIND_RUN)
from .store import new_id, now_str

# ---------------------------------------------------------------- 时间换算


def hhmm_to_min(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def min_to_hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def now_minutes() -> int:
    n = datetime.now()
    return n.hour * 60 + n.minute


def parse_date(s: str) -> dt_date:
    return datetime.strptime(s, "%Y-%m-%d").date()


# ---------------------------------------------------------------- 教学周


def week_of(date_str: str, settings: dict) -> Tuple[int, str]:
    """返回 (第几周, "单周"/"双周")。基准 = 设置里的学期第一周周一。"""
    start = parse_date(settings.get("semester_start", "2026-08-31"))
    d = parse_date(date_str)
    week_no = (d - start).days // 7 + 1
    return week_no, ("单周" if week_no % 2 == 1 else "双周")


# ---------------------------------------------------------------- 固定安排与课隙


def fixed_blocks_for_date(date_str: str, courses: List[Course],
                          events: List[Event],
                          settings: Optional[dict] = None) -> List[dict]:
    """当天的固定安排（课程[单双周过滤] + 临时活动 + 三餐午休），按开始时间排序。"""
    d = parse_date(date_str)
    blocks: List[dict] = []

    week_type_today: Optional[str] = None
    if settings:
        _, week_type_today = week_of(date_str, settings)

    for c in courses:
        if c.weekday != d.weekday():
            continue
        wt = getattr(c, "week_type", "每周") or "每周"
        if week_type_today and wt not in ("每周", week_type_today):
            continue  # 单双周不符，今天没这门课
        blocks.append({
            "kind": "课程", "name": c.name,
            "start": hhmm_to_min(c.start), "end": hhmm_to_min(c.end),
            "location": c.location, "heavy": c.heavy,
            "subject": c.subject or c.name,
        })
    for e in events:
        if e.date == date_str:
            blocks.append({
                "kind": "活动", "name": e.title,
                "start": hhmm_to_min(e.start), "end": hhmm_to_min(e.end),
                "location": e.location, "heavy": False, "subject": "",
            })

    if settings:
        blocks += _meal_blocks(settings, blocks)

    blocks.sort(key=lambda b: (b["start"], b["end"]))
    return blocks


def _meal_blocks(settings: dict, existing: List[dict]) -> List[dict]:
    """三餐与午休。晚餐按规则：傍晚有课→课后时段；没课→空闲时段。"""
    meals = settings.get("meals", {})
    out: List[dict] = []

    def _mk(key: str, kind: str, name: str) -> Optional[dict]:
        t = meals.get(key)
        if not t:
            return None
        return {"kind": kind, "name": name,
                "start": hhmm_to_min(t[0]), "end": hhmm_to_min(t[1]),
                "location": "", "heavy": False, "subject": ""}

    for key, kind, name in (("breakfast", "用餐", "早餐"),
                            ("lunch", "用餐", "午餐"),
                            ("nap", "午休", "午休")):
        b = _mk(key, kind, name)
        if b:
            out.append(b)

    # 晚餐：傍晚（17:00-17:20）有课 → 用"有课"时段；否则用"没课"时段
    dinner = None
    if meals.get("dinner_if_class") and meals.get("dinner_if_free"):
        has_late_class = any(
            b["kind"] == "课程" and b["start"] < hhmm_to_min("17:20")
            and b["end"] > hhmm_to_min("17:00") for b in existing)
        key = "dinner_if_class" if has_late_class else "dinner_if_free"
        dinner = _mk(key, "用餐", "晚餐")
    if dinner:
        out.append(dinner)
    return out


def compute_gaps(date_str: str, blocks: List[dict], settings: dict,
                 now_min: Optional[int] = None) -> List[dict]:
    """计算当天两项固定安排之间的课隙。

    返回列表，每项：start/end（分钟）、minutes、usable（扣除过渡时间后）、
    prev_block/next_block、too_short（不足 15 分钟，默认用于移动/休息）、
    past（在当前时间之前或进行中且剩余不足）。
    """
    day_start = hhmm_to_min(settings["day_start"])
    day_end = hhmm_to_min(settings["day_end"])
    transition = int(settings["transition_minutes"])
    min_gap = int(settings["min_gap"])

    # 把固定安排裁剪到规划窗口内并排序
    span = [b for b in blocks if b["end"] > day_start and b["start"] < day_end]
    span = [{**b, "start": max(b["start"], day_start),
             "end": min(b["end"], day_end)} for b in span]
    span.sort(key=lambda b: b["start"])

    gaps: List[dict] = []
    cursor = day_start
    prev_block: Optional[dict] = None
    for b in span:
        if b["start"] > cursor:
            gaps.append({"start": cursor, "end": b["start"],
                         "prev_block": prev_block, "next_block": b})
        cursor = max(cursor, b["end"])
        prev_block = b
    if cursor < day_end:
        gaps.append({"start": cursor, "end": day_end,
                     "prev_block": prev_block, "next_block": None})

    for g in gaps:
        g["minutes"] = g["end"] - g["start"]
        g["usable"] = g["minutes"] - transition
        g["too_short"] = g["minutes"] < min_gap
        # 时间过滤：课隙须在当前时间之后，且剩余部分仍够最短课隙
        if now_min is not None and g["end"] <= now_min + min_gap:
            g["past"] = True
        else:
            g["past"] = False
            if now_min is not None and g["start"] < now_min:
                g["start"] = now_min  # 进行中的课隙，从当前时刻起算
                g["minutes"] = g["end"] - g["start"]
                g["usable"] = g["minutes"] - transition
    return gaps


# ---------------------------------------------------------------- 打分

_DIFF_NUM = {"低": 1, "中": 2, "高": 3}


def _urgency(task: Task, today: dt_date) -> Tuple[float, dict]:
    """紧急度：截止越近、剩余越多，分越高。"""
    days_left = max(0, (parse_date(task.deadline) - today).days)
    deadline_factor = 1.0 if days_left == 0 else max(0.0, 1.0 - days_left / 7.0)
    done_min = sum(s.minutes for s in task.steps
                   if s.status not in (STEP_PENDING, STEP_MISSED, "已安排"))
    remaining = max(0, task.total_minutes - done_min)
    progress_factor = remaining / task.total_minutes if task.total_minutes else 0.0
    score = min(1.0, 0.65 * deadline_factor + 0.35 * progress_factor)
    return score, {"days_left": days_left, "remaining": remaining}


def _fit(step_min: int, usable: int) -> float:
    """时长匹配：步骤与可用时间越贴合越高；微任务进短课隙，长课隙留给长任务。"""
    if step_min <= 0 or usable <= 0:
        return 0.0
    return min(step_min, usable) / max(step_min, usable)


def _energy_match(difficulty: str, energy: str) -> float:
    """精力匹配：高精力日难步骤优先；低精力日轻任务优先。"""
    d = _DIFF_NUM.get(difficulty, 2)
    if energy == "高":
        return d / 3.0
    if energy == "低":
        return (4 - d) / 3.0
    return 1.0 - abs(d - 2) / 2.0


def _switch(prev_block: Optional[dict], difficulty: str,
            prev_subject: str, task: Task) -> Tuple[float, List[str]]:
    """负担与切换：费脑课后首个课隙轻任务加分；同学科连排加分。"""
    score, notes = 0.3, []
    if prev_block and prev_block.get("heavy") and difficulty == "低":
        score += 0.4
        notes.append(f"刚上完费脑课「{prev_block['name']}」，轻任务缓冲")
    if prev_subject and prev_subject == (task.name or ""):
        score += 0.4
        notes.append("与前一安排同学科，减少切换")
    return min(1.0, score), notes


def score_step(step: Step, task: Task, usable: int, energy: str,
               prev_block: Optional[dict], prev_subject: str,
               today: dt_date, weights: Dict[str, float]) -> Tuple[float, dict]:
    u, uinfo = _urgency(task, today)
    f = _fit(step.minutes, usable)
    e = _energy_match(task.difficulty, energy)
    s, snotes = _switch(prev_block, task.difficulty, prev_subject, task)
    total = (weights.get("urgency", 0.4) * u + weights.get("fit", 0.25) * f +
             weights.get("energy", 0.2) * e + weights.get("switch", 0.15) * s)
    parts = {"紧急度": round(u, 2), "时长匹配": round(f, 2),
             "精力匹配": round(e, 2), "负担切换": round(s, 2),
             "days_left": uinfo["days_left"], "switch_notes": snotes}
    return total, parts


# ---------------------------------------------------------------- 理由生成


def _deadline_text(days_left: int, task: Task) -> str:
    progress = ""
    done = sum(s.minutes for s in task.steps
               if s.status not in (STEP_PENDING, STEP_MISSED, "已安排"))
    if task.total_minutes and done < task.total_minutes / 2:
        progress = "（剩余进度不足一半）"
    if days_left == 0:
        return f"今天截止{progress}"
    if days_left == 1:
        return f"明天截止{progress}"
    if days_left == 2:
        return f"后天截止{progress}"
    return f"{days_left} 天后截止{progress}"


def build_reason(task: Task, step: Step, usable: int, transition: int,
                 energy: str, parts: dict, rest_kept: int) -> str:
    """套模板生成具体理由。"""
    seg = [f"{task.name}·{step.name}预计 {step.minutes} 分钟，"
           f"{_deadline_text(parts['days_left'], task)}，"
           f"此课隙扣除 {transition} 分钟过渡后可用 {usable} 分钟"]
    if parts["时长匹配"] >= 0.7:
        seg.append("时间贴合")
    elif parts["时长匹配"] >= 0.4:
        seg.append("时间基本够用")
    else:
        seg.append("可用时间充裕，便于从容完成")
    if energy == "高" and task.difficulty == "高":
        seg.append("高精力状态优先攻克难步骤")
    if energy == "低" and task.difficulty == "低":
        seg.append("低精力状态安排轻任务")
    seg.extend(parts.get("switch_notes", []))
    if rest_kept >= 10:
        seg.append(f"保留 {rest_kept} 分钟休息")
    return "，".join(seg) + "，故安排在此。"


# ---------------------------------------------------------------- 校园跑


def run_status(data: dict, date_str: str, settings: dict) -> dict:
    """校园跑进度：学期累计、本周次数、是否落后进度、本周应跑次数。"""
    rc = settings.get("run", {})
    week_no, _ = week_of(date_str, settings)
    w0, w1 = rc.get("semester_weeks", [2, 16])
    done_dates = list(data.get("runs", {}).get("done_dates", []))
    done_total = len(done_dates)

    d = parse_date(date_str)
    monday = d - timedelta(days=d.weekday())
    week_dates = {(monday + timedelta(days=i)).strftime("%Y-%m-%d")
                  for i in range(7)}
    this_week_done = sum(1 for x in done_dates if x in week_dates)
    this_week_planned = 0
    for pd, plan in data.get("plans", {}).items():
        if pd in week_dates and pd != date_str:
            this_week_planned += sum(
                1 for i in plan.get("items", [])
                if i.get("kind") == KIND_RUN and i.get("status") != "跳过")

    base = rc.get("weekly_base", 2)
    # 到上周末为止"应完成"的次数（每周 base 次）
    expected = base * max(0, week_no - w0)
    behind = done_total < expected
    weekly_target = rc.get("weekly_catchup", 3) if behind else base
    weekdays = (rc.get("weekdays_catchup", [1, 3, 5]) if behind
                else rc.get("weekdays_base", [1, 3]))
    return {
        "in_season": w0 <= week_no <= w1,
        "week_no": week_no,
        "done_total": done_total,
        "target_total": rc.get("semester_target", 30),
        "this_week": this_week_done + this_week_planned,
        "weekly_target": weekly_target,
        "behind": behind,
        "weekdays": weekdays,
        "done_dates": done_dates,
        "minutes": rc.get("minutes", 30),
        "after": hhmm_to_min(rc.get("after", "18:00")),
    }


def _place_run(items: List[PlanItem], gaps: List[dict], ri: dict,
               transition: int) -> Optional[PlanItem]:
    """把校园跑放进当天最晚的晚间空档末尾（跑完就是一天最后一项）。"""
    run_min = ri["minutes"]
    evening = [g for g in gaps
               if not g["past"] and not g["too_short"]
               and g["end"] > ri["after"] and g["usable"] >= run_min]
    for g in sorted(evening, key=lambda x: x["end"], reverse=True):
        slot_start = max(g["end"] - run_min, ri["after"])
        if slot_start < g["start"] + transition:
            continue
        occupied = [hhmm_to_min(i.end) for i in items
                    if hhmm_to_min(i.start) >= g["start"]
                    and hhmm_to_min(i.start) < g["end"]]
        earliest_free = max(occupied) if occupied else g["start"] + transition
        if slot_start >= earliest_free:
            nth = ri["this_week"] + 1
            return PlanItem(
                id=new_id(), task_id="", step_id="",
                task_name="校园跑", step_name=f"{run_min} 分钟校园跑",
                start=min_to_hhmm(slot_start), end=min_to_hhmm(g["end"]),
                reason=(f"学期进度 {ri['done_total']}/{ri['target_total']} 次，"
                        f"本周第 {nth} 次（目标 {ri['weekly_target']} 次）；"
                        f"安排在一天最后一项，跑完直接洗澡休息。"),
                kind=KIND_RUN)
    return None


# ---------------------------------------------------------------- 编排主流程


def generate_plan(date_str: str, energy: str, tasks: List[Task],
                  courses: List[Course], events: List[Event], settings: dict,
                  now_min: Optional[int] = None,
                  keep_items: Optional[List[PlanItem]] = None,
                  skip_step_ids: Optional[set] = None,
                  run_info: Optional[dict] = None) -> dict:
    """生成当日课隙计划。

    keep_items：重排时要原样保留的计划项（已完成/已开始/锁定/过去时段，
    仅学习任务；休息与校园跑每次重新生成）。
    run_info：run_status() 的结果，提供则会尝试安排校园跑。
    返回 dict：items / gaps / notes / risks / 统计信息。
    """
    today = parse_date(date_str)
    weights = settings["weights"]
    transition = int(settings["transition_minutes"])
    min_gap = int(settings["min_gap"])
    cap_ratio = float(settings["caps"].get(energy, 0.65))
    pomo = settings.get("pomodoro",
                        {"study": 25, "short_break": 5,
                         "long_break": 15, "long_every": 4})
    p_study = int(pomo["study"])

    blocks = fixed_blocks_for_date(date_str, courses, events, settings)
    gaps = compute_gaps(date_str, blocks, settings, now_min)
    usable_gaps = [g for g in gaps if not g["past"] and not g["too_short"]]

    keep_items = keep_items or []
    skip_step_ids = skip_step_ids or set()
    kept_step_ids = {i.step_id for i in keep_items if i.step_id}

    # 候选步骤池：未完成、未跳过、不在保留项里、未被显式排除
    pool: List[Tuple[Step, Task]] = []
    for t in tasks:
        for s in t.steps:
            if s.status in (STEP_PENDING, STEP_MISSED, "已安排") \
                    and s.id not in kept_step_ids and s.id not in skip_step_ids:
                # 背诵的间隔复习：建议日期之前不排
                if s.suggest_offset:
                    created = parse_date(t.created_at[:10]) if t.created_at else today
                    if (today - created).days < s.suggest_offset:
                        continue
                pool.append((s, t))

    # 当日留白上限：学习任务总占用 ≤ cap × 全部可用课隙（休息不计入）
    total_usable = sum(max(0, g["usable"]) for g in usable_gaps)
    cap_minutes = int(total_usable * cap_ratio)
    used = sum(hhmm_to_min(i.end) - hhmm_to_min(i.start)
               for i in keep_items if i.kind == KIND_TASK)

    items: List[PlanItem] = list(keep_items)
    notes: List[str] = []
    assigned: set = set()
    prev_subject = ""

    # 番茄节奏状态（由已保留的学习项推算）
    sessions_done = used // p_study
    session_min = used % p_study

    for g in usable_gaps:
        # 课程/用餐/午休等 ≥25 分钟的固定块本身就是长中断，番茄计数清零
        if g["prev_block"] and \
                (g["prev_block"]["end"] - g["prev_block"]["start"]) >= p_study:
            session_min = 0
        remaining = g["usable"]
        cursor = g["start"] + transition  # 课隙开头先留过渡时间
        gap_assigned = False

        while remaining >= min_gap:
            # —— 番茄休息：连续学习满一个番茄，先插休息再排下一段 ——
            if session_min >= p_study:
                sessions_done += 1
                long_now = sessions_done % int(pomo["long_every"]) == 0
                rest = int(pomo["long_break"]) if long_now \
                    else int(pomo["short_break"])
                label = "大休息" if long_now else "休息"
                if remaining >= rest:
                    items.append(PlanItem(
                        id=new_id(), task_id="", step_id="",
                        task_name=label, step_name=f"{rest} 分钟{label}",
                        start=min_to_hhmm(cursor), end=min_to_hhmm(cursor + rest),
                        reason=(f"已连续学习约 {session_min} 分钟，"
                                f"按番茄节奏安排 {rest} 分钟{label}。"),
                        kind=KIND_REST))
                    cursor += rest
                    remaining -= rest
                    session_min = 0
                    continue
                else:
                    notes.append(
                        f"{min_to_hhmm(g['start'])}–{min_to_hhmm(g['end'])} "
                        f"课隙末尾不足 {rest} 分钟，不再安排，正好休息。")
                    break

            best, best_score, best_parts = None, -1.0, {}
            for (s, t) in pool:
                if s.id in assigned or s.minutes > remaining:
                    continue
                sc, parts = score_step(s, t, remaining, energy,
                                       g["prev_block"], prev_subject,
                                       today, weights)
                if sc > best_score:
                    best, best_score, best_parts = (s, t), sc, parts
            if not best:
                break
            # 硬约束 5：留白上限（任何状态不排满；只计学习任务）
            if used + best[0].minutes > cap_minutes:
                notes.append(
                    f"{min_to_hhmm(g['start'])}–{min_to_hhmm(g['end'])} 课隙"
                    f"已达留白上限（{energy}精力 ≤{int(cap_ratio * 100)}%），"
                    f"不再安排，保留休息。")
                break
            s, t = best
            start, end = cursor, cursor + s.minutes
            rest_kept = g["end"] - end
            reason = build_reason(t, s, g["usable"], transition, energy,
                                  best_parts, max(0, rest_kept))
            items.append(PlanItem(
                id=new_id(), task_id=t.id, step_id=s.id,
                task_name=t.name, step_name=s.name,
                start=min_to_hhmm(start), end=min_to_hhmm(end),
                reason=reason, status=ITEM_PLANNED,
                score_parts={k: v for k, v in best_parts.items()
                             if k in ("紧急度", "时长匹配", "精力匹配", "负担切换")},
            ))
            assigned.add(s.id)
            used += s.minutes
            session_min += s.minutes
            prev_subject = t.name
            cursor = end
            remaining = g["end"] - cursor
            gap_assigned = True

        if not gap_assigned and g["usable"] >= min_gap:
            notes.append(
                f"{min_to_hhmm(g['start'])}–{min_to_hhmm(g['end'])} 课隙留白："
                f"没有时长合适的剩余步骤，可用于休息或机动。")

    # —— 校园跑：当天最后一项 ——
    run_note = ""
    if run_info and run_info["in_season"]:
        weekday = today.weekday()
        if (weekday in run_info["weekdays"]
                and run_info["this_week"] < run_info["weekly_target"]
                and date_str not in run_info["done_dates"]):
            run_item = _place_run(items, gaps, run_info, transition)
            if run_item:
                items.append(run_item)
            else:
                run_note = ("今天晚间没有连续 30 分钟空档，校园跑未安排，"
                            "本周后续日子会补上。")

    items.sort(key=lambda i: i.start)
    if run_note:
        notes.append(run_note)

    # 截止风险：临近截止但仍有步骤排不进课隙
    risks: List[str] = []
    for (s, t) in pool:
        if s.id in assigned or s.id in kept_step_ids:
            continue
        days_left = (parse_date(t.deadline) - today).days
        if days_left <= 1:
            risks.append(
                f"「{t.name}·{s.name}」（{s.minutes} 分钟）今天/明天截止，"
                f"但今天的课隙排不下，建议尽早找整块时间完成。")
    seen, uniq_risks = set(), []
    for r in risks:
        if r not in seen:
            uniq_risks.append(r)
            seen.add(r)

    return {"items": items, "gaps": gaps, "notes": notes,
            "risks": uniq_risks, "cap_minutes": cap_minutes,
            "total_usable": total_usable, "generated_at": now_str()}


# ---------------------------------------------------------------- 局部重排


def replan(date_str: str, energy: str, old_items: List[PlanItem],
           tasks: List[Task], courses: List[Course], events: List[Event],
           settings: dict, now_min: int, trigger: str,
           changed_step_ids: Optional[set] = None,
           run_info: Optional[dict] = None) -> dict:
    """局部重排：只动当前时间之后、未完成、未锁定的学习安排。

    休息项与校园跑项不保留，随新方案重新生成。
    返回新编排结果 + changes（变更说明列表）。
    """
    keep: List[PlanItem] = []
    released: List[PlanItem] = []
    for it in old_items:
        if it.kind != KIND_TASK:
            continue  # 休息/校园跑：不保留，重新生成
        end_m = hhmm_to_min(it.end)
        if it.locked or it.status in ("已完成",) or end_m <= now_min:
            keep.append(it)          # 已完成/锁定/已过去：不动
        else:
            released.append(it)      # 未来且未完成：放回候选池重排

    result = generate_plan(date_str, energy, tasks, courses, events,
                           settings, now_min=now_min, keep_items=keep,
                           skip_step_ids=changed_step_ids, run_info=run_info)

    # 变更说明：哪些移动、原位置、新位置、原因、截止风险
    new_by_step = {i.step_id: i for i in result["items"] if i.step_id}
    changes: List[str] = []
    for old in released:
        new = new_by_step.get(old.step_id)
        if new is None:
            changes.append(f"「{old.task_name}·{old.step_name}」原计划 "
                           f"{old.start}–{old.end}，本次未排入课隙，"
                           f"回到待安排池。")
        elif new.start != old.start:
            changes.append(f"「{old.task_name}·{old.step_name}」从 "
                           f"{old.start}–{old.end} 调整到 {new.start}–{new.end}。")
    kept_ids = {i.step_id for i in keep} | {o.step_id for o in released}
    for it in result["items"]:
        if it.kind == KIND_TASK and it.step_id not in kept_ids:
            changes.append(f"「{it.task_name}·{it.step_name}」新安排在 "
                           f"{it.start}–{it.end}。")
    if not changes:
        changes.append("重排后安排无变化。")
    header = f"触发原因：{trigger}。只调整当前时间之后、未完成且未锁定的安排。"
    result["changes"] = [header] + changes
    return result
