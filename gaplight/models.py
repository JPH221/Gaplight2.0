"""隙光 GapLight —— 数据模型定义。

所有时间在存储层一律使用字符串：
- 时刻："HH:MM"（24 小时制）
- 日期："YYYY-MM-DD"
换算成"分钟数"的计算只在 scheduler 内部进行。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
ENERGY_LEVELS = ["低", "中", "高"]
DIFFICULTIES = ["低", "中", "高"]
TASK_TYPES = ["课程复习", "作业", "阅读", "背诵", "自定义"]
WEEK_TYPES = ["每周", "单周", "双周"]

# 步骤 / 计划项状态常量
STEP_PENDING = "待安排"
STEP_SCHEDULED = "已安排"
STEP_DONE = "已完成"
STEP_SKIPPED = "跳过"
STEP_MISSED = "未完成"

ITEM_PLANNED = "计划"
ITEM_DONE = "已完成"
ITEM_SKIPPED = "跳过"
ITEM_MISSED = "未完成"

# 计划项类型
KIND_TASK = "task"    # 学习任务
KIND_REST = "rest"    # 番茄休息（自动生成）
KIND_RUN = "run"      # 校园跑


@dataclass
class Course:
    """每学期录入一次的固定课程，分每周/单周/双周。"""
    id: str
    name: str
    weekday: int            # 0=周一 … 6=周日
    start: str              # "HH:MM"
    end: str                # "HH:MM"
    location: str = ""      # 默认空白，由用户填写
    heavy: bool = False     # 是否为费脑课程（影响"负担与切换"打分）
    subject: str = ""       # 学科名（同学科连排加分），留空时取课程名
    week_type: str = "每周"  # 每周 / 单周 / 双周

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Course":
        return Course(**d)


@dataclass
class Event:
    """一次性临时安排：会议 / 社团活动 / 调课等。"""
    id: str
    title: str
    date: str               # "YYYY-MM-DD"
    start: str
    end: str
    location: str = "校内"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Event":
        return Event(**d)


@dataclass
class Step:
    """任务拆分后的一个步骤。"""
    id: str
    name: str
    minutes: int
    status: str = STEP_PENDING
    suggest_offset: int = 0  # 建议相对任务创建日推迟几天做（背诵的间隔复习用）

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Step":
        return Step(**d)


@dataclass
class Task:
    """用户任务。必填：名称 / 截止时间 / 预计总时长。"""
    id: str
    name: str
    type: str               # TASK_TYPES 之一
    deadline: str           # "YYYY-MM-DD"
    total_minutes: int
    difficulty: str = "中"
    location: str = "校内任意"
    custom: bool = False    # 自定义任务：不套模板、不拆分
    created_at: str = ""
    steps: List[Step] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @staticmethod
    def from_dict(d: dict) -> "Task":
        d = dict(d)
        d["steps"] = [Step.from_dict(s) for s in d.get("steps", [])]
        return Task(**d)

    def remaining_steps(self) -> List[Step]:
        """还未完成、未跳过的步骤（待安排 + 已安排 + 未完成）。"""
        return [s for s in self.steps
                if s.status in (STEP_PENDING, STEP_SCHEDULED, STEP_MISSED)]

    def remaining_minutes(self) -> int:
        return sum(s.minutes for s in self.remaining_steps())


@dataclass
class PlanItem:
    """当日计划中的一项安排。"""
    id: str
    task_id: str
    step_id: str
    task_name: str
    step_name: str
    start: str              # "HH:MM"
    end: str
    reason: str = ""        # 编排理由（程序套模板生成）
    status: str = ITEM_PLANNED
    locked: bool = False    # 锁定后重排不动它
    score_parts: dict = field(default_factory=dict)  # 四因素得分明细
    kind: str = KIND_TASK   # task / rest / run

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "PlanItem":
        return PlanItem(**d)


@dataclass
class DayPlan:
    """某一天的课隙计划。"""
    date: str               # "YYYY-MM-DD"
    energy: str             # 低 / 中 / 高
    items: List[PlanItem] = field(default_factory=list)
    copy: str = ""          # 当日舒缓文案（随计划保存）
    copy_enabled: bool = True
    created_at: str = ""
    collab_note: str = ""   # 已废弃字段（v2 起移除连小理协作），仅为兼容旧数据保留

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "DayPlan":
        d = dict(d)
        d["items"] = [PlanItem.from_dict(i) for i in d.get("items", [])]
        return DayPlan(**d)
