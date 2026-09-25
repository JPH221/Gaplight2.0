"""隙光 GapLight —— 本地 JSON 持久化与实验日志。

- 数据文件：data/gaplight_data.json（课表、活动、任务、计划、设置）
- 日志文件：data/gaplight_log.jsonl（每行一条事件，含操作时间，供实验指标人工计算）
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import List, Optional

from .models import Course, Event, Task, DayPlan

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DATA_FILE = os.path.join(DATA_DIR, "gaplight_data.json")
LOG_FILE = os.path.join(DATA_DIR, "gaplight_log.jsonl")

# 可调参数默认值（设置页可修改，便于真实使用中校准）
DEFAULT_SETTINGS = {
    "weights": {"urgency": 0.40, "fit": 0.25, "energy": 0.20, "switch": 0.15},
    "caps": {"低": 0.50, "中": 0.65, "高": 0.75},  # 留白上限：任务总占用不超过该比例
    "transition_minutes": 10,   # 每个课隙先扣的过渡时间
    "day_start": "08:00",       # 规划窗口
    "day_end": "21:30",
    "min_gap": 15,              # 最短可安排课隙
    # —— v2 新增 ——
    "semester_start": "2026-08-31",  # 本学期第一周周一（单双周基准）
    "meals": {                       # 每日固定餐饮/午休（早餐在规划窗口前）
        "breakfast": ["07:30", "07:45"],
        "lunch": ["11:50", "12:15"],
        "nap": ["12:50", "13:10"],
        "dinner_if_class": ["17:20", "17:45"],  # 傍晚有课时
        "dinner_if_free": ["17:30", "18:00"],   # 傍晚没课时（17:00~18:30 内挑 30 分钟）
    },
    "pomodoro": {"study": 25, "short_break": 5, "long_break": 15, "long_every": 4},
    "run": {                         # 校园跑
        "minutes": 30,
        "after": "18:00",            # 不早于此时间
        "semester_weeks": [2, 16],   # 第 2~16 周需要跑步
        "semester_target": 30,       # 学期累计至少 30 次
        "weekly_base": 2,            # 正常每周 2 次
        "weekly_catchup": 3,         # 进度落后时每周 3 次
        "weekdays_base": [1, 3],     # 周二、周四（0=周一）
        "weekdays_catchup": [1, 3, 5],  # 落后时加周六
    },
}

_EMPTY = {
    "courses": [],
    "events": [],
    "tasks": [],
    "plans": {},
    "settings": DEFAULT_SETTINGS,
    "copy_index": 0,
    "copy_enabled": True,
    "runs": {"done_dates": []},      # 校园跑完成记录（v2）
}


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def load_data() -> dict:
    """读取全部数据；文件不存在时返回空结构。"""
    _ensure_dir()
    if not os.path.exists(DATA_FILE):
        return json.loads(json.dumps(_EMPTY))
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # 补齐缺省键，避免旧版本数据缺字段
    for k, v in _EMPTY.items():
        data.setdefault(k, json.loads(json.dumps(v)))
    for k, v in DEFAULT_SETTINGS.items():
        data["settings"].setdefault(k, json.loads(json.dumps(v)))
    # v2 迁移：旧版默认 18:00 结束 → 21:30
    if data["settings"].get("day_end") == "18:00":
        data["settings"]["day_end"] = "21:30"
    # v2 迁移：旧课程补 week_type
    for c in data["courses"]:
        c.setdefault("week_type", "每周")
    return data


def save_data(data: dict) -> None:
    _ensure_dir()
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------- 各类对象的读写辅助 ----------

def get_courses(data: dict) -> List[Course]:
    return [Course.from_dict(d) for d in data["courses"]]


def get_events(data: dict) -> List[Event]:
    return [Event.from_dict(d) for d in data["events"]]


def get_tasks(data: dict) -> List[Task]:
    return [Task.from_dict(d) for d in data["tasks"]]


def get_plan(data: dict, date_str: str) -> Optional[DayPlan]:
    d = data["plans"].get(date_str)
    return DayPlan.from_dict(d) if d else None


def put_plan(data: dict, plan: DayPlan) -> None:
    data["plans"][plan.date] = plan.to_dict()


def save_courses(data: dict, courses: List[Course]) -> None:
    data["courses"] = [c.to_dict() for c in courses]


def save_events(data: dict, events: List[Event]) -> None:
    data["events"] = [e.to_dict() for e in events]


def save_tasks(data: dict, tasks: List[Task]) -> None:
    data["tasks"] = [t.to_dict() for t in tasks]


# ---------- 实验日志（只追加，不修改） ----------

def log_event(event_type: str, payload: dict) -> None:
    """记录一条实验事件：推荐/接受/修改/拒绝/完成/跳过/重排/录入等。"""
    _ensure_dir()
    rec = {"time": now_str(), "type": event_type, "payload": payload}
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_logs(limit: Optional[int] = None) -> List[dict]:
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    recs = [json.loads(ln) for ln in lines]
    if limit:
        recs = recs[-limit:]
    return recs
