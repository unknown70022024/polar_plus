"""
light.sources.blitzortung — Blitzortung 全球闪电网格（第三方 JSON-RPC 镜像）。

服务：wuan/bo-android 项目维护的公开 JSON-RPC（http://bo-service.tryb.de/）。

实测结论（2026-09-17）：
  * 方法 get_global_strikes_grid(minute_length, grid_base, minute_offset,
    threshold) 返回全球网格，响应约 64 KB。
  * 方法 get_strikes_grid(minute_length, grid_base) 返回**硬编码的欧洲区域**
    （lon -25..57 / lat 27..72），第二参数只影响分辨率，区域恒定。
  * **服务不支持按经纬度取数**，所以亚太只能在客户端过滤。

响应字段：
  r  [[x_idx, y_idx, count, time_offset], ...]
  xd 每格经度（度）   yd 每格纬度（度）
  x0/y1/xc/yc 网格原点与尺寸（元数据，索引换算用不到）
  t  快照时间        dt 窗口秒数      h 每 1/12 窗口的直方图

索引换算（与现有 polar_plus/fetch_storms.py 一致）：
  lon = (x_idx + 0.5) * xd     归一化到 [-180, 180)
  lat = -(y_idx + 0.5) * yd
"""
from __future__ import annotations

import json
import logging
import math
import random
import urllib.error
import urllib.request
from datetime import datetime, timezone

from polar_plus.lightning import config

logger = logging.getLogger(__name__)

# 每个网格单元最多产出多少"等效点"。count 只用于给融合器排序，
# 不需要真的展开成 count 个点，否则一个 500 次的格子会灌进 500 个坐标。
_MAX_POINTS_PER_CELL = 20


def _call(minute_offset: int, window_minutes: int) -> dict | None:
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": config.BO_METHOD,
        "params": [window_minutes, config.BO_GRID_BASE, minute_offset,
                   config.BO_THRESHOLD],
        "id": 1,
    }).encode()

    try:
        req = urllib.request.Request(config.BO_SERVICE_URL, data=payload,
                                     headers=config.BO_HEADERS)
        with urllib.request.urlopen(req, timeout=config.BO_TIMEOUT) as resp:
            body = resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        logger.warning("bo-service 请求失败: %s: %s", type(exc).__name__, exc)
        return None

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        logger.warning("bo-service 响应不是 JSON: %s", exc)
        return None

    result = data.get("result")
    if not isinstance(result, dict) or "r" not in result:
        logger.warning("bo-service 响应缺少 result/r: %s",
                       str(data.get("error", data))[:160])
        return None
    return result


def fetch(dt_end: datetime, window_minutes: int) -> list[tuple[float, float, str, float]]:
    """取 Blitzortung 数据，过滤到亚太区。

    Returns:
        [(lat, lon, "blitzortung", weight), ...]；失败时返回空列表。
        weight = 该网格单元的雷击次数（用于融合排序）。
    """
    now = datetime.now(timezone.utc)
    if dt_end.tzinfo is None:
        dt_end = dt_end.replace(tzinfo=timezone.utc)
    minute_offset = int((dt_end - now).total_seconds() / 60.0)

    logger.info("Blitzortung: 请求 %d 分钟窗口, offset=%d 分钟",
                window_minutes, minute_offset)
    result = _call(minute_offset, window_minutes)
    if result is None:
        return []

    xd = float(result.get("xd") or 0.0)
    yd = float(result.get("yd") or 0.0)
    cells = result.get("r") or []
    if not xd or not yd or not cells:
        logger.warning("Blitzortung: 网格参数异常或为空 (xd=%s yd=%s cells=%d)",
                       xd, yd, len(cells))
        return []

    total = sum(int(c[2]) for c in cells)
    lo0, lo1, la0, la1 = config.BLITZ_BBOX

    pts: list[tuple[float, float, str, float]] = []
    kept_strikes = 0
    for cell in cells:
        try:
            x_idx, y_idx, count = int(cell[0]), int(cell[1]), int(cell[2])
        except (IndexError, TypeError, ValueError):
            continue
        if count <= 0:
            continue

        lon = (x_idx + 0.5) * xd
        lon = ((lon + 180.0) % 360.0) - 180.0
        lat = -(y_idx + 0.5) * yd

        if not config.blitz_in_bbox(lat, lon):
            continue
        kept_strikes += count

        # 单元内抖动出若干等效点；数量以 count 封顶，避免把 1 次雷击
        # 也画成 20 个点。
        n = max(1, min(count, _MAX_POINTS_PER_CELL))
        for _ in range(n):
            jl = lon + random.uniform(-xd / 2, xd / 2)
            jb = lat + random.uniform(-yd / 2, yd / 2)
            jl = ((jl + 180.0) % 360.0) - 180.0
            jb = max(-90.0, min(90.0, jb))
            pts.append((jb, jl, "blitzortung", count / float(n)))

    logger.info("Blitzortung: 全球 %d 次 / %d 格 -> 亚太框 lon %.0f..%.0f lat %.0f..%.0f "
                "保留 %d 次 / %d 格 -> %d 个点",
                total, len(cells), lo0, lo1, la0, la1,
                kept_strikes, len({(round(p[1], 3), round(p[0], 3)) for p in pts}),
                len(pts))
    return pts
