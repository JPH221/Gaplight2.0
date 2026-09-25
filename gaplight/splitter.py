"""隙光 GapLight —— 任务拆分模板（4 类已定稿 + 自定义）。

通用规则（v2 起按番茄节奏调整）：
- 每步 15~25 分钟（25 分钟一个番茄，长步骤按 25 分钟切段）；
- 总时长 ≤25 分钟不拆；
- 拆后固定显示假设声明（见 ASSUMPTION 常量），每一步可改名、改时长、删除。

| 类型     | 拆分规则                                                     |
|----------|--------------------------------------------------------------|
| 课程复习 | 梳理笔记 → 做例题（约 4:6；超 25 分钟切段）                  |
| 作业     | 完成主体 → 检查订正提交（约 8:2；超 25 分钟切段）            |
| 阅读     | 纯等长切块，每块 25 分钟，无附加环节                          |
| 背诵     | 首次背诵（较长）＋两次间隔短复习（建议分到后续不同天）        |
| 自定义   | 不拆分，整块（≤25 分钟；超时提示用户手动拆成多个任务）        |
"""
from __future__ import annotations

from typing import List

from .models import Step
from .store import new_id

ASSUMPTION = "这是按通用学习方法给出的建议拆分，每一步你都可以改名、改时长或删除。"

MIN_STEP = 15
MAX_STEP = 25  # v2：番茄钟节奏，一个番茄 25 分钟


def _r5(x: float) -> int:
    """四舍五入到最近的 5 分钟。"""
    return max(5, int(round(x / 5.0)) * 5)


def _mk(name: str, minutes: int, offset: int = 0) -> Step:
    return Step(id=new_id(), name=name, minutes=int(minutes), suggest_offset=offset)


def _split_two(name: str, minutes: int, labels=("（上）", "（下）")) -> List[Step]:
    """把一段时间切成两段；若单段仍超 40 分钟则递归均分并编号。"""
    if minutes <= MAX_STEP:
        return [_mk(name, minutes)]
    half = _r5(minutes / 2)
    a, b = half, minutes - half
    if b > MAX_STEP:
        # 仍然超长：均分为 n 段，每段 ≤25（一个番茄）
        n = (minutes + MAX_STEP - 1) // MAX_STEP
        base = _r5(minutes / n)
        parts, rest = [], minutes
        for i in range(n - 1):
            parts.append(base)
            rest -= base
        parts.append(rest)
        return [_mk(f"{name}（{i + 1}）", p) for i, p in enumerate(parts)]
    return [_mk(f"{name}{labels[0]}", a), _mk(f"{name}{labels[1]}", b)]


def split_task(task_type: str, total_minutes: int) -> List[Step]:
    """按模板把任务拆成步骤列表。总时长 ≤25 分钟一律不拆。"""
    total = int(total_minutes)
    if total <= MAX_STEP:
        return [_mk("完成全部", total)]

    if task_type == "课程复习":
        note = _r5(total * 0.4)
        exercise = total - note
        steps: List[Step] = []
        steps += _split_two("梳理笔记", note) if note > MAX_STEP else [_mk("梳理笔记", note)]
        steps += _split_two("做例题", exercise)
        return steps

    if task_type == "作业":
        body = _r5(total * 0.8)
        check = total - body
        if check < 10:  # 检查环节太短就并回主体
            body, check = total, 0
        steps = _split_two("完成主体", body)
        if check:
            steps.append(_mk("检查订正提交", check))
        return steps

    if task_type == "阅读":
        # 等长切块，每块 25 分钟（一个番茄）
        n = max(2, round(total / MAX_STEP))
        base = _r5(total / n)
        base = min(MAX_STEP, max(MIN_STEP, base))
        parts, rest = [], total
        for _ in range(n - 1):
            parts.append(base)
            rest -= base
        parts.append(rest)
        return [_mk(f"阅读第 {i + 1} 块", p) for i, p in enumerate(parts)]

    if task_type == "背诵":
        first = _r5(total * 0.55)
        first = min(MAX_STEP, max(20, first))
        rest = total - first
        review = max(MIN_STEP, _r5(rest / 2))
        # 两次间隔短复习：建议分到后续不同天（+1 天、+3 天，间隔重复）
        return [
            _mk("首次背诵", first, offset=0),
            _mk("第一次间隔复习", review, offset=1),
            _mk("第二次间隔复习", review, offset=3),
        ]

    # 自定义：不拆分（>25 分钟由界面提示用户手动拆成多个任务）
    return [_mk("完成全部", total)]


def needs_manual_split(task_type: str, total_minutes: int) -> bool:
    """自定义任务超过 25 分钟时，提示用户手动拆成多个任务。"""
    return task_type == "自定义" and int(total_minutes) > MAX_STEP
