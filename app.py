"""隙光 GapLight —— 面向大学新生的自适应课隙规划。

本地运行的 Streamlit 应用。运行方式：
    streamlit run app.py
"""
from __future__ import annotations

from datetime import datetime, date as dt_date

import streamlit as st

from gaplight import store, splitter, copytext, exportlog
from gaplight.models import (Course, Event, Task, PlanItem, DayPlan,
                             WEEKDAYS, ENERGY_LEVELS, DIFFICULTIES, TASK_TYPES,
                             WEEK_TYPES, KIND_TASK, KIND_REST, KIND_RUN,
                             STEP_PENDING, STEP_SCHEDULED, STEP_DONE,
                             STEP_SKIPPED, STEP_MISSED,
                             ITEM_PLANNED, ITEM_DONE, ITEM_SKIPPED, ITEM_MISSED)
from gaplight.scheduler import (fixed_blocks_for_date, compute_gaps,
                                generate_plan, replan, run_status, week_of,
                                hhmm_to_min, min_to_hhmm, now_minutes,
                                parse_date)

st.set_page_config(page_title="隙光 GapLight", page_icon="🌤", layout="wide")

TODAY = dt_date.today().strftime("%Y-%m-%d")

REPLAN_TRIGGERS = [
    "临时活动冲突", "标记未完成或跳过", "实际耗时超计划",
    "临近截止进度不足", "修改当日精力", "用户主动要求",
]

STATUS_ICON = {ITEM_PLANNED: "🕒", ITEM_DONE: "✅",
               ITEM_SKIPPED: "⏭️", ITEM_MISSED: "⚠️"}


# ---------------------------------------------------------------- 数据辅助


def load() -> dict:
    return store.load_data()


def save(data: dict) -> None:
    store.save_data(data)


def make_proposal(data: dict, date_str: str, trigger: str) -> None:
    """基于当前数据生成一份"剩余日程新方案"，挂到待确认区。"""
    tasks = store.get_tasks(data)
    courses = store.get_courses(data)
    events = store.get_events(data)
    plan = store.get_plan(data, date_str)
    now_min = now_minutes() if date_str == TODAY else None
    if plan and plan.items:
        result = replan(date_str, plan.energy, plan.items, tasks, courses,
                        events, data["settings"],
                        now_min or 0, trigger,
                        run_info=run_status(data, date_str, data["settings"]))
        st.session_state["proposal"] = {
            "date": date_str, "energy": plan.energy, "result": result,
        }
        store.log_event("重排", {"date": date_str, "trigger": trigger,
                               "changes": result.get("changes", [])})
    else:
        energy = st.session_state.get("energy_pick", "中")
        result = generate_plan(date_str, energy, tasks, courses, events,
                               data["settings"], now_min=now_min,
                               run_info=run_status(data, date_str,
                                                   data["settings"]))
        result["changes"] = [f"触发原因：{trigger}。首次生成当日计划。"]
        st.session_state["proposal"] = {
            "date": date_str, "energy": energy, "result": result,
        }
        store.log_event("推荐", {"date": date_str, "energy": energy,
                               "items": len(result["items"])})


def apply_proposal(data: dict, proposal: dict) -> None:
    """确认：新计划替换旧计划，并同步步骤状态。"""
    date_str = proposal["date"]
    result = proposal["result"]
    old = store.get_plan(data, date_str)
    plan = DayPlan(date=date_str, energy=proposal["energy"],
                   items=result["items"],
                   copy=old.copy if old else "",
                   copy_enabled=old.copy_enabled if old else True,
                   created_at=store.now_str(),
                   collab_note=old.collab_note if old else "")
    if not plan.copy:
        plan.copy = copytext.get_copy(data.get("copy_index", 0))
    store.put_plan(data, plan)

    # 同步步骤状态：进入计划 → 已安排；离开计划且未完成 → 待安排
    scheduled_ids = {i.step_id for i in plan.items if i.step_id}
    tasks = store.get_tasks(data)
    for t in tasks:
        for s in t.steps:
            if s.id in scheduled_ids:
                s.status = STEP_SCHEDULED
            elif s.status == STEP_SCHEDULED:
                s.status = STEP_PENDING
    store.save_tasks(data, tasks)
    store.log_event("接受", {"date": date_str, "items": len(plan.items),
                           "changes": result.get("changes", [])})


def reject_proposal(data: dict, proposal: dict) -> None:
    """拒绝：保持原计划不变。"""
    store.log_event("拒绝", {"date": proposal["date"],
                           "changes": proposal["result"].get("changes", [])})


def set_item_status(data: dict, date_str: str, item: PlanItem,
                    status: str) -> None:
    """更新计划项与对应步骤的状态并记录日志。"""
    plan = store.get_plan(data, date_str)
    if not plan:
        return
    for it in plan.items:
        if it.id == item.id:
            it.status = status
    store.put_plan(data, plan)
    # 校园跑完成 → 计入学期进度
    if item.kind == KIND_RUN and status == ITEM_DONE:
        done_dates = data.setdefault("runs", {}).setdefault("done_dates", [])
        if date_str not in done_dates:
            done_dates.append(date_str)
    step_status = {ITEM_DONE: STEP_DONE, ITEM_SKIPPED: STEP_SKIPPED,
                   ITEM_MISSED: STEP_MISSED}.get(status)
    if step_status and item.step_id:
        tasks = store.get_tasks(data)
        for t in tasks:
            if t.id == item.task_id:
                for s in t.steps:
                    if s.id == item.step_id:
                        s.status = step_status
        store.save_tasks(data, tasks)
    store.log_event(status, {"date": date_str, "task": item.task_name,
                            "step": item.step_name,
                            "slot": f"{item.start}–{item.end}"})


# ---------------------------------------------------------------- 页面：课表与活动


def page_schedule(data: dict) -> None:
    st.header("🏫 课表与活动")
    st.caption("一学期录入一次课表（课程分 每周 / 单周 / 双周）；临时会议、社团、调课可单独添加，不必重录整份课表。")
    tab1, tab2 = st.tabs(["每周课表", "临时活动"])

    with tab1:
        with st.form("add_course", clear_on_submit=True):
            c1, c2, c3, c4 = st.columns(4)
            name = c1.text_input("课程名称", placeholder="如：高等数学")
            weekday = c2.selectbox("星期", range(7),
                                   format_func=lambda i: WEEKDAYS[i])
            start = c3.time_input("开始时间", value=datetime(2026, 9, 24, 8, 0).time())
            end = c4.time_input("结束时间", value=datetime(2026, 9, 24, 9, 35).time())
            c5, c6, c7, c8 = st.columns(4)
            location = c5.text_input("地点", value="", placeholder="如：综二 301")
            subject = c6.text_input("学科（选填，用于同学科连排加分）")
            week_type = c7.selectbox("周次", WEEK_TYPES)
            heavy = c8.checkbox("费脑课程（如高数/大物）", value=False)
            if st.form_submit_button("➕ 添加课程"):
                if not name:
                    st.warning("请填写课程名称。")
                elif end <= start:
                    st.warning("结束时间需晚于开始时间。")
                else:
                    courses = store.get_courses(data)
                    courses.append(Course(
                        id=store.new_id(), name=name, weekday=weekday,
                        start=start.strftime("%H:%M"), end=end.strftime("%H:%M"),
                        location=location, heavy=heavy,
                        subject=subject or name, week_type=week_type))
                    store.save_courses(data, courses)
                    save(data)
                    store.log_event("录入课表", {"name": name,
                                               "weekday": WEEKDAYS[weekday],
                                               "week_type": week_type})
                    st.rerun()

        courses = store.get_courses(data)
        if not courses:
            st.info("还没有课程，先录入本周课表吧。")
        for wd in range(7):
            day_courses = [c for c in courses if c.weekday == wd]
            if not day_courses:
                continue
            st.subheader(WEEKDAYS[wd])
            for c in sorted(day_courses, key=lambda x: x.start):
                cols = st.columns([3, 2, 2, 1])
                tag = "🧠" if c.heavy else "📘"
                wt = getattr(c, "week_type", "每周") or "每周"
                cols[0].write(f"{tag} **{c.name}**")
                cols[1].write(f"{c.start}–{c.end}")
                cols[2].write(f"{c.location or '—'}｜{wt}")
                if cols[3].button("删除", key=f"del_c_{c.id}"):
                    store.log_event("删除课表", {"name": c.name})
                    courses = [x for x in courses if x.id != c.id]
                    store.save_courses(data, courses)
                    save(data)
                    st.rerun()

    with tab2:
        with st.form("add_event", clear_on_submit=True):
            c1, c2 = st.columns(2)
            title = c1.text_input("活动名称", placeholder="如：社团例会 / 调课补课")
            day = c2.date_input("日期", value=dt_date.today())
            c3, c4, c5 = st.columns(3)
            start = c3.time_input("开始时间", value=datetime(2026, 9, 24, 16, 0).time())
            end = c4.time_input("结束时间", value=datetime(2026, 9, 24, 17, 0).time())
            location = c5.text_input("地点 ", value="校内")
            if st.form_submit_button("➕ 添加活动"):
                if not title:
                    st.warning("请填写活动名称。")
                elif end <= start:
                    st.warning("结束时间需晚于开始时间。")
                else:
                    events = store.get_events(data)
                    events.append(Event(
                        id=store.new_id(), title=title,
                        date=day.strftime("%Y-%m-%d"),
                        start=start.strftime("%H:%M"), end=end.strftime("%H:%M"),
                        location=location or "校内"))
                    store.save_events(data, events)
                    save(data)
                    store.log_event("录入活动", {"title": title,
                                               "date": day.strftime("%Y-%m-%d")})
                    # 临时活动冲突 → 触发局部重排提案
                    if day.strftime("%Y-%m-%d") == TODAY and store.get_plan(data, TODAY):
                        make_proposal(data, TODAY, f"临时活动冲突：{title}")
                        st.toast("检测到与今日计划可能冲突，已生成重排提案，请到「今日计划」确认。")
                    st.rerun()

        events = sorted(store.get_events(data),
                        key=lambda e: (e.date, e.start), reverse=True)
        if not events:
            st.info("暂无临时活动。")
        for e in events:
            cols = st.columns([2, 3, 2, 2, 1])
            cols[0].write(e.date)
            cols[1].write(f"**{e.title}**")
            cols[2].write(f"{e.start}–{e.end}")
            cols[3].write(e.location)
            if cols[4].button("删除", key=f"del_e_{e.id}"):
                store.log_event("删除活动", {"title": e.title})
                events = [x for x in events if x.id != e.id]
                store.save_events(data, events)
                save(data)
                st.rerun()


# ---------------------------------------------------------------- 页面：任务管理


def page_tasks(data: dict) -> None:
    st.header("✅ 任务管理")
    st.caption("必填：名称 / 截止时间 / 预计总时长；选填：难度（默认中等）、地点（默认校内任意）。")

    with st.expander("➕ 添加新任务", expanded=True):
        with st.form("add_task", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            name = c1.text_input("任务名称", placeholder="如：高数第三章作业")
            ttype = c2.selectbox("类型", TASK_TYPES)
            deadline = c3.date_input("截止时间", value=dt_date.today())
            c4, c5, c6 = st.columns(3)
            total = c4.number_input("预计总时长（分钟）", min_value=5, max_value=600,
                                    value=60, step=5)
            difficulty = c5.selectbox("难度", DIFFICULTIES, index=1)
            location = c6.text_input("地点", value="校内任意")
            submitted = st.form_submit_button("添加并拆分")
        if submitted:
            if not name:
                st.warning("请填写任务名称。")
            else:
                steps = splitter.split_task(ttype, int(total))
                task = Task(id=store.new_id(), name=name, type=ttype,
                            deadline=deadline.strftime("%Y-%m-%d"),
                            total_minutes=int(total), difficulty=difficulty,
                            location=location or "校内任意",
                            custom=(ttype == "自定义"),
                            created_at=store.now_str(), steps=steps)
                tasks = store.get_tasks(data)
                tasks.append(task)
                store.save_tasks(data, tasks)
                save(data)
                store.log_event("录入任务", {"name": name, "type": ttype,
                                           "total": int(total),
                                           "deadline": task.deadline,
                                           "steps": len(steps)})
                if splitter.needs_manual_split(ttype, int(total)):
                    st.warning("自定义任务超过 25 分钟，建议手动拆成多个任务添加（一个番茄 25 分钟）。")
                # 新任务添加后：自动生成新的剩余日程方案 → 用户确认才替换
                make_proposal(data, TODAY, f"新任务：{name}")
                st.toast("已生成新的剩余日程方案，请到「今日计划」查看变更说明并确认。")
                st.rerun()

    tasks = store.get_tasks(data)
    if not tasks:
        st.info("还没有任务。添加后会按类型自动建议拆分。")
        return

    for t in tasks:
        remaining = t.remaining_minutes()
        done = t.total_minutes - remaining
        with st.expander(f"📌 {t.name}｜{t.type}｜截止 {t.deadline}｜"
                         f"已完成 {done}/{t.total_minutes} 分钟"):
            st.write(f"难度：{t.difficulty}　地点：{t.location}　"
                     f"添加时间：{t.created_at}")
            st.caption("💡 " + splitter.ASSUMPTION)
            # 步骤编辑：每步可改名 / 改时长 / 删除
            new_steps = []
            for s in t.steps:
                cols = st.columns([3, 1.5, 1.5, 1])
                sname = cols[0].text_input("步骤名", value=s.name,
                                           key=f"sn_{s.id}",
                                           label_visibility="collapsed")
                smin = cols[1].number_input("分钟", min_value=5, max_value=240,
                                            value=s.minutes, step=5,
                                            key=f"sm_{s.id}",
                                            label_visibility="collapsed")
                cols[2].write(s.status + (f"（建议+{s.suggest_offset}天）"
                                          if s.suggest_offset else ""))
                drop = cols[3].checkbox("删", key=f"sd_{s.id}")
                if not drop:
                    s.name, s.minutes = sname, int(smin)
                    new_steps.append(s)
            c1, c2 = st.columns(2)
            if c1.button("💾 保存修改", key=f"save_t_{t.id}"):
                t.steps = new_steps
                t.total_minutes = sum(s.minutes for s in new_steps)
                all_tasks = store.get_tasks(data)
                for i, x in enumerate(all_tasks):
                    if x.id == t.id:
                        all_tasks[i] = t
                store.save_tasks(data, all_tasks)
                save(data)
                store.log_event("修改", {"task": t.name,
                                       "steps": len(t.steps)})
                st.success("已保存。")
                st.rerun()
            if c2.button("🗑 删除整个任务", key=f"del_t_{t.id}"):
                store.log_event("删除任务", {"task": t.name})
                tasks = [x for x in tasks if x.id != t.id]
                store.save_tasks(data, tasks)
                save(data)
                st.rerun()


# ---------------------------------------------------------------- 页面：今日计划


def _render_proposal(data: dict) -> None:
    """展示待确认的新方案：变更说明 + 一键确认 / 拒绝。"""
    proposal = st.session_state.get("proposal")
    if not proposal:
        return
    result = proposal["result"]
    st.subheader("📋 新的日程方案（待确认）")
    st.warning("新方案尚未生效。确认后替换当前计划，拒绝则保持原计划。")
    st.markdown("**变更说明**")
    for line in result.get("changes", []):
        st.write("· " + line)
    for r in result.get("risks", []):
        st.error("⚠️ 截止风险：" + r)
    if result.get("notes"):
        with st.expander("留白说明"):
            for n in result["notes"]:
                st.write("· " + n)
    with st.expander(f"新方案明细（{len(result['items'])} 项）"):
        for i in result["items"]:
            st.write(f"**{i.start}–{i.end}**　{i.task_name}·{i.step_name}"
                     f"{'　🔒' if i.locked else ''}")
            st.caption("理由：" + i.reason)
    c1, c2 = st.columns(2)
    if c1.button("✅ 确认采用新方案", type="primary", use_container_width=True):
        apply_proposal(data, proposal)
        save(data)
        del st.session_state["proposal"]
        st.rerun()
    if c2.button("❌ 拒绝，保持原计划", use_container_width=True):
        reject_proposal(data, proposal)
        save(data)
        del st.session_state["proposal"]
        st.rerun()
    st.divider()


def page_today(data: dict) -> None:
    week_no, parity = week_of(TODAY, data["settings"])
    st.header(f"🌞 今日计划（{TODAY} · 第 {week_no} 周 · {parity}）")

    # 校园跑学期进度
    ri = run_status(data, TODAY, data["settings"])
    if ri["in_season"]:
        st.caption(f"🏃 校园跑：学期 {ri['done_total']}/{ri['target_total']} 次　"
                   f"本周 {ri['this_week']}/{ri['weekly_target']} 次"
                   + ("　⚠️ 进度落后，本周加跑一次" if ri["behind"] else ""))

    # 待确认提案优先展示
    _render_proposal(data)

    plan = store.get_plan(data, TODAY)

    # 首次使用：选择精力状态
    if not plan:
        st.subheader("今天感觉状态怎么样？")
        energy = st.radio("选择今天的精力状态", ENERGY_LEVELS, index=1,
                          horizontal=True, key="energy_pick")
        st.caption("精力状态决定留白上限：低精力 ≤50%｜中精力 ≤65%｜高精力 ≤75%，"
                   "任何状态都不会排满。")
        if st.button("🧭 生成今日计划", type="primary"):
            make_proposal(data, TODAY, "首次生成今日计划")
            save(data)
            st.rerun()
        _render_gaps(data, TODAY)
        return

    # 每日舒缓文案（随计划保存，可更换 / 关闭）
    if plan.copy_enabled and plan.copy:
        c1, c2, c3 = st.columns([6, 1, 1])
        c1.info(f"🌿 {plan.copy}")
        if c2.button("换一句"):
            data["copy_index"] = data.get("copy_index", 0) + 1
            plan.copy = copytext.get_copy(data["copy_index"])
            store.put_plan(data, plan)
            save(data)
            st.rerun()
        if c3.button("关闭"):
            plan.copy_enabled = False
            store.put_plan(data, plan)
            save(data)
            st.rerun()

    # 精力状态与重排
    st.write(f"**当日精力：{plan.energy}**　计划生成于 {plan.created_at}")
    with st.expander("🔧 触发重排"):
        c1, c2 = st.columns(2)
        trigger = c1.selectbox("触发原因", REPLAN_TRIGGERS)
        new_energy = c2.selectbox("修改精力为（可选）", ["不修改"] + ENERGY_LEVELS)
        if st.button("♻️ 生成重排方案"):
            trig = trigger
            if new_energy != "不修改" and new_energy != plan.energy:
                trig = f"修改当日精力：{plan.energy}→{new_energy}（{trigger}）"
                plan.energy = new_energy
                store.put_plan(data, plan)
            make_proposal(data, TODAY, trig)
            save(data)
            st.rerun()

    # 计划明细
    st.subheader("📅 今日安排")
    if not plan.items:
        st.write("今天目前没有安排任何任务，课隙全部留白。")
    now_min = now_minutes()
    for it in plan.items:
        # 番茄休息项：轻量展示，无操作按钮
        if it.kind == KIND_REST:
            st.markdown(f"🍅 `{it.start}–{it.end}`　*{it.step_name}* — {it.reason}")
            continue
        is_run = it.kind == KIND_RUN
        icon = "🏃" if is_run else STATUS_ICON.get(it.status, "🕒")
        with st.container(border=True):
            cols = st.columns([2, 4, 2])
            cols[0].write(f"**{it.start}–{it.end}**")
            cols[1].write(f"{icon} **{it.task_name}** · {it.step_name}"
                          f"{'　🔒 已锁定' if it.locked else ''}　_{it.status}_")
            if it.score_parts:
                cols[2].write("得分明细：")
                cols[2].caption("　".join(f"{k} {v}"
                                          for k, v in it.score_parts.items()))
            st.caption("💬 " + it.reason)
            if it.status == ITEM_PLANNED:
                b1, b2, b3, b4, _ = st.columns([1, 1, 1, 1, 3])
                if b1.button("✅ 完成", key=f"done_{it.id}"):
                    set_item_status(data, TODAY, it, ITEM_DONE)
                    save(data)
                    st.rerun()
                if not is_run:
                    if b2.button("⚠️ 未完成", key=f"miss_{it.id}"):
                        set_item_status(data, TODAY, it, ITEM_MISSED)
                        # 标记未完成 → 自动触发重排提案
                        make_proposal(data, TODAY,
                                      f"标记未完成：{it.task_name}·{it.step_name}")
                        save(data)
                        st.rerun()
                if b3.button("⏭️ 跳过", key=f"skip_{it.id}"):
                    set_item_status(data, TODAY, it, ITEM_SKIPPED)
                    make_proposal(data, TODAY,
                                  f"标记跳过：{it.task_name}·{it.step_name}")
                    save(data)
                    st.rerun()
                if not is_run and b4.button("🔒 锁定" if not it.locked else "🔓 解锁",
                                            key=f"lock_{it.id}"):
                    it.locked = not it.locked
                    plan2 = store.get_plan(data, TODAY)
                    for x in plan2.items:
                        if x.id == it.id:
                            x.locked = it.locked
                    store.put_plan(data, plan2)
                    save(data)
                    store.log_event("锁定" if it.locked else "解锁",
                                    {"task": it.task_name, "step": it.step_name})
                    st.rerun()
            if not is_run and it.status == ITEM_PLANNED \
                    and hhmm_to_min(it.end) <= now_min:
                st.caption("⏰ 该时段已过去，可标记完成 / 未完成 / 跳过。")

    _render_gaps(data, TODAY)


def _render_gaps(data: dict, date_str: str) -> None:
    """课隙总览：可用时间、留白与未安排原因、兜底风险提示。"""
    courses = store.get_courses(data)
    events = store.get_events(data)
    settings = data["settings"]
    blocks = fixed_blocks_for_date(date_str, courses, events, settings)
    now_min = now_minutes() if date_str == TODAY else None
    gaps = compute_gaps(date_str, blocks, settings, now_min)
    with st.expander("🕳 今日课隙总览（每隙先扣 "
                     f"{settings['transition_minutes']} 分钟过渡时间）"):
        if not blocks:
            st.write("今天没有固定安排。")
        rows = []
        for g in gaps:
            state = "已过" if g["past"] else (
                "不足 15 分钟，默认移动/休息" if g["too_short"]
                else f"可安排（可用 {max(0, g['usable'])} 分钟）")
            rows.append({
                "课隙": f"{min_to_hhmm(g['start'])}–{min_to_hhmm(g['end'])}",
                "长度": f"{g['minutes']} 分钟",
                "前一安排": g["prev_block"]["name"] if g["prev_block"] else "—",
                "后一安排": g["next_block"]["name"] if g["next_block"] else "—",
                "状态": state,
            })
        st.table(rows)


# ---------------------------------------------------------------- 页面：记录与导出


def page_records(data: dict) -> None:
    st.header("📊 记录与导出")
    st.caption("程序自动记录：推荐 / 接受 / 修改 / 拒绝 / 完成 / 跳过 / 重排 / 操作时间。"
               "6 项指标由人工根据原始记录计算（见 docs/指标定义附录.md），程序不做自动统计面板。")

    logs = store.read_logs()
    st.write(f"共 **{len(logs)}** 条原始记录。")
    if logs:
        c1, c2 = st.columns(2)
        c1.download_button("⬇️ 导出 JSONL（原始记录）",
                           exportlog.logs_to_jsonl(logs),
                           file_name="gaplight_log.jsonl",
                           mime="application/jsonl")
        c2.download_button("⬇️ 导出 CSV",
                           exportlog.logs_to_csv(logs),
                           file_name="gaplight_log.csv", mime="text/csv")
        st.subheader("最近记录")
        for r in reversed(logs[-100:]):
            st.write(f"`{r['time']}`　**{r['type']}**　"
                     f"{_brief(r.get('payload', {}))}")
    else:
        st.info("还没有记录。开始使用后会自动记录每一次操作。")

    st.divider()
    st.markdown("**实验原则**：主动休息 ≠ 浪费；不把休息包装成效率提升；"
                "学习占比如实呈现；保留原始记录与计算方法。"
                "功能演示 / 压力测试使用虚拟场景时，须在界面与材料中明确标注，"
                "不得冒充真实数据。")


def _brief(payload: dict) -> str:
    parts = []
    for k in ("task", "step", "name", "title", "trigger", "date", "slot"):
        if k in payload:
            parts.append(str(payload[k]))
    return "　".join(parts) if parts else "—"


# ---------------------------------------------------------------- 页面：设置


def page_settings(data: dict) -> None:
    st.header("⚙️ 设置（可调参数）")
    st.caption("权重与上限为可调参数，便于真实使用中校准。修改后对之后生成的计划生效。")
    s = data["settings"]

    st.subheader("四因素权重")
    c1, c2, c3, c4 = st.columns(4)
    w_u = c1.slider("紧急度", 0.0, 1.0, float(s["weights"]["urgency"]), 0.05)
    w_f = c2.slider("时长匹配", 0.0, 1.0, float(s["weights"]["fit"]), 0.05)
    w_e = c3.slider("精力匹配", 0.0, 1.0, float(s["weights"]["energy"]), 0.05)
    w_s = c4.slider("负担与切换", 0.0, 1.0, float(s["weights"]["switch"]), 0.05)
    total_w = w_u + w_f + w_e + w_s
    if abs(total_w - 1.0) > 0.01:
        st.warning(f"当前权重合计 {total_w:.2f}，保存时将自动归一化为 1。")

    st.subheader("留白上限（任务总占用不超过课隙可用时间的比例）")
    c1, c2, c3 = st.columns(3)
    cap1 = c1.slider("低精力", 0.2, 0.9, float(s["caps"]["低"]), 0.05)
    cap2 = c2.slider("中精力", 0.2, 0.9, float(s["caps"]["中"]), 0.05)
    cap3 = c3.slider("高精力", 0.2, 0.9, float(s["caps"]["高"]), 0.05)

    st.subheader("时间与课隙")
    c1, c2, c3, c4 = st.columns(4)
    t_start = c1.time_input("规划窗口开始",
                            value=datetime.strptime(s["day_start"], "%H:%M").time())
    t_end = c2.time_input("规划窗口结束",
                          value=datetime.strptime(s["day_end"], "%H:%M").time())
    trans = c3.number_input("每课隙过渡时间（分钟）", min_value=0, max_value=30,
                            value=int(s["transition_minutes"]))
    min_gap = c4.number_input("最短可安排课隙（分钟）", min_value=5, max_value=60,
                              value=int(s["min_gap"]))

    st.subheader("学期与三餐作息")
    meals = s.get("meals", {})

    def _mt(key, default):
        return datetime.strptime(meals.get(key, default)[0], "%H:%M").time(), \
               datetime.strptime(meals.get(key, default)[1], "%H:%M").time()

    sem = st.date_input("本学期第一周周一（单双周基准）",
                        value=parse_date(s.get("semester_start", "2026-08-31")))
    c1, c2, c3, c4 = st.columns(4)
    lu = meals.get("lunch", ["11:50", "12:15"])
    lunch_s = c1.time_input("午餐开始", value=datetime.strptime(lu[0], "%H:%M").time())
    lunch_e = c2.time_input("午餐结束", value=datetime.strptime(lu[1], "%H:%M").time())
    np_ = meals.get("nap", ["12:50", "13:10"])
    nap_s = c3.time_input("午休开始", value=datetime.strptime(np_[0], "%H:%M").time())
    nap_e = c4.time_input("午休结束", value=datetime.strptime(np_[1], "%H:%M").time())
    c1, c2 = st.columns(2)
    dc = meals.get("dinner_if_class", ["17:20", "17:45"])
    df = meals.get("dinner_if_free", ["17:30", "18:00"])
    dc_s = c1.time_input("晚餐（傍晚有课）开始",
                         value=datetime.strptime(dc[0], "%H:%M").time())
    dc_e = c2.time_input("晚餐（傍晚有课）结束",
                         value=datetime.strptime(dc[1], "%H:%M").time())
    c1, c2 = st.columns(2)
    df_s = c1.time_input("晚餐（傍晚没课）开始",
                         value=datetime.strptime(df[0], "%H:%M").time())
    df_e = c2.time_input("晚餐（傍晚没课）结束",
                         value=datetime.strptime(df[1], "%H:%M").time())
    st.caption("番茄节奏固定为：学习 25 分钟 → 休息 5 分钟，每 4 个番茄后大休息 15 分钟。"
               "校园跑规则：第 2~16 周累计 ≥30 次，每周 2 次（落后自动 3 次），18:00 后安排为当天最后一项。")

    if st.button("💾 保存设置", type="primary"):
        if t_end <= t_start:
            st.error("规划窗口结束时间需晚于开始时间。")
            return
        tw = total_w if total_w > 0 else 1.0
        s["weights"] = {"urgency": round(w_u / tw, 3), "fit": round(w_f / tw, 3),
                        "energy": round(w_e / tw, 3), "switch": round(w_s / tw, 3)}
        s["caps"] = {"低": cap1, "中": cap2, "高": cap3}
        s["transition_minutes"] = int(trans)
        s["min_gap"] = int(min_gap)
        s["day_start"] = t_start.strftime("%H:%M")
        s["day_end"] = t_end.strftime("%H:%M")
        s["semester_start"] = sem.strftime("%Y-%m-%d")
        s["meals"] = {
            "breakfast": meals.get("breakfast", ["07:30", "07:45"]),
            "lunch": [lunch_s.strftime("%H:%M"), lunch_e.strftime("%H:%M")],
            "nap": [nap_s.strftime("%H:%M"), nap_e.strftime("%H:%M")],
            "dinner_if_class": [dc_s.strftime("%H:%M"), dc_e.strftime("%H:%M")],
            "dinner_if_free": [df_s.strftime("%H:%M"), df_e.strftime("%H:%M")],
        }
        save(data)
        store.log_event("修改设置", s)
        st.success("设置已保存。")


# ---------------------------------------------------------------- 主入口


def main() -> None:
    st.sidebar.title("🌤 隙光 GapLight")
    st.sidebar.caption("面向大学新生的自适应课隙规划")
    st.sidebar.markdown("开发者：JH")
    page = st.sidebar.radio("功能", [
        "今日计划", "课表与活动", "任务管理",
        "记录与导出", "设置",
    ])
    st.sidebar.divider()
    st.sidebar.caption("内置规则编排：硬约束 + 四因素打分 + 番茄节奏 + 留白上限。")

    data = load()
    if page == "今日计划":
        page_today(data)
    elif page == "课表与活动":
        page_schedule(data)
    elif page == "任务管理":
        page_tasks(data)
    elif page == "记录与导出":
        page_records(data)
    else:
        page_settings(data)


main()
