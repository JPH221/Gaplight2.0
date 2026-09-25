"""隙光 GapLight —— 实验原始记录导出。

程序自动记录：推荐/接受/修改/拒绝/完成/跳过/重排/操作时间。
6 项指标由人工根据原始记录计算（见 docs/指标定义附录.md），
程序不做指标自动统计面板。
"""
from __future__ import annotations

import json
from typing import List


def logs_to_jsonl(records: List[dict]) -> str:
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in records)


def logs_to_csv(records: List[dict]) -> str:
    """压平成 CSV：时间,事件类型,摘要(JSON)。"""
    lines = ["time,type,payload"]
    for r in records:
        payload = json.dumps(r.get("payload", {}), ensure_ascii=False)
        payload = '"' + payload.replace('"', '""') + '"'
        lines.append(f"{r.get('time','')},{r.get('type','')},{payload}")
    return "\n".join(lines)
