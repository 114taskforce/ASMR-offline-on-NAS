"""API 分时禁用 —— 在规定的时间窗内不发起任何大模型调用。

配置挂在 ``subtitle.api_offpeak``：

    {"enabled": true,
     "windows": [{"days": [1, 2, 3, 4, 5], "start": "09:00", "end": "12:00"}, ...]}

``days`` 取 1..7（周一=1），使用宿主机本地时区。窗口允许跨天（start > end）。
"""
from __future__ import annotations

import datetime as _dt
import re

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _minutes(value) -> int | None:
    m = _HHMM.match(str(value or "").strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def default_schedule() -> dict:
    return {
        "enabled": False,
        "windows": [
            {"days": [1, 2, 3, 4, 5], "start": "09:00", "end": "12:00"},
            {"days": [1, 2, 3, 4, 5], "start": "14:00", "end": "18:00"},
        ],
    }


def normalize_windows(windows) -> list[dict]:
    """校验并规范化时间窗；非法项直接丢弃，保证返回结构可直接存库。"""
    out: list[dict] = []
    for raw in windows or []:
        if not isinstance(raw, dict):
            continue
        days: list[int] = []
        for d in raw.get("days") or []:
            try:
                day = int(d)
            except (TypeError, ValueError):
                continue
            if 1 <= day <= 7 and day not in days:
                days.append(day)
        start, end = _minutes(raw.get("start")), _minutes(raw.get("end"))
        if not days or start is None or end is None or start == end:
            continue
        out.append({"days": sorted(days), "start": _fmt(start), "end": _fmt(end)})
    return out


def get_schedule(cfg: dict) -> dict:
    sub = cfg.get("subtitle") or {}
    raw = sub.get("api_offpeak")
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": bool(raw.get("enabled")),
        "windows": normalize_windows(raw.get("windows")),
    }


def is_api_blocked(cfg: dict, now: _dt.datetime | None = None) -> bool:
    """当前是否处于「禁止调用 API」的时间窗内。"""
    sched = get_schedule(cfg)
    if not sched["enabled"] or not sched["windows"]:
        return False
    now = now or _dt.datetime.now()
    weekday = now.isoweekday()
    minute = now.hour * 60 + now.minute
    for w in sched["windows"]:
        if weekday not in w["days"]:
            continue
        start = _minutes(w["start"])
        end = _minutes(w["end"])
        if start is None or end is None:
            continue
        if start < end:
            if start <= minute < end:
                return True
        else:  # 跨天窗口
            if minute >= start or minute < end:
                return True
    return False


def next_open_at(cfg: dict, now: _dt.datetime | None = None) -> _dt.datetime | None:
    """下一次允许调用 API 的时刻；当前未禁用时返回 None。"""
    sched = get_schedule(cfg)
    if not sched["enabled"] or not sched["windows"]:
        return None
    now = now or _dt.datetime.now()
    if not is_api_blocked(cfg, now):
        return None
    # 逐分钟向后找最近的非禁用时刻（最多 8 天）
    probe = now.replace(second=0, microsecond=0)
    for _ in range(8 * 24 * 60):
        probe += _dt.timedelta(minutes=1)
        if not is_api_blocked(cfg, probe):
            return probe
    return None
