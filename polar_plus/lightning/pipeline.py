"""
light.pipeline — 三源闪电融合的入口。

    python -m light.pipeline

环境变量：
    LIGHT_SOURCES=blitzortung,glm,eumetsat   要启用的源（默认全开）
    LIGHT_MAX_POINTS=2500                    输出点上限
    LIGHT_WINDOW_MINUTES=60                  时间窗长度
    LIGHT_BLITZ_LON_MIN/LON_MAX/LAT_MIN/LAT_MAX   Blitzortung 保留区域
    GCC_TIMESTAMP=YYYYMMDD_HHMMSS            对齐到云图时刻（可选）
    OUTPUT_DIR=...                           输出目录
    EUMETSAT_CONSUMER_KEY / EUMETSAT_CONSUMER_SECRET

降级策略：任一源失败只是少一部分点，不影响其他源；全部失败时回退到
40 个硬编码城市坐标（这是刻意设计的行为，保留）。
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polar_plus.lightning import config, fuse
from polar_plus.lightning.sources import blitzortung, eumetsat_li, goes_glm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("polar_plus.lightning.pipeline")

# 全部源都失败时的回退坐标。这是原管线的既有行为，刻意保留。
DEFAULT_LOCATIONS = [
    {"lat": 34.05, "lng": -118.24}, {"lat": -33.87, "lng": 151.21},
    {"lat": 51.51, "lng": -0.13},   {"lat": 35.68, "lng": 139.76},
    {"lat": -34.60, "lng": -58.38}, {"lat": 41.01, "lng": 28.98},
    {"lat": 19.08, "lng": 72.88},   {"lat": -1.29, "lng": 36.82},
    {"lat": 55.75, "lng": 37.62},   {"lat": -22.91, "lng": -43.20},
    {"lat": 30.04, "lng": 31.24},   {"lat": -6.21, "lng": 106.85},
    {"lat": 48.86, "lng": 2.35},    {"lat": -37.81, "lng": 144.96},
    {"lat": 37.57, "lng": 126.98},  {"lat": 14.60, "lng": 120.98},
    {"lat": -4.33, "lng": 15.31},   {"lat": 25.20, "lng": 55.27},
    {"lat": 40.42, "lng": -3.70},   {"lat": 52.52, "lng": 13.41},
    {"lat": 59.33, "lng": 18.07},   {"lat": 33.89, "lng": 35.50},
    {"lat": -26.20, "lng": 28.05},  {"lat": 53.55, "lng": -113.49},
    {"lat": 43.65, "lng": -79.38},  {"lat": -12.05, "lng": -77.04},
    {"lat": 39.90, "lng": 116.41},  {"lat": -31.95, "lng": 115.86},
    {"lat": 47.38, "lng": 8.54},    {"lat": 60.17, "lng": 24.94},
    {"lat": 38.72, "lng": -9.14},   {"lat": 50.85, "lng": 4.35},
    {"lat": 52.37, "lng": 4.89},    {"lat": 45.44, "lng": 9.19},
    {"lat": 17.39, "lng": 78.49},   {"lat": 29.56, "lng": 106.55},
    {"lat": 44.80, "lng": 20.47},   {"lat": -23.55, "lng": -46.63},
    {"lat": 28.61, "lng": 77.23},   {"lat": 13.75, "lng": 100.50},
]


def resolve_window() -> tuple[datetime, str]:
    """确定时间窗终点：优先对齐云图时间戳，否则用当前整点。"""
    ts = (os.environ.get("GCC_TIMESTAMP") or "").strip()
    if not ts:
        p = Path(os.environ.get("OUTPUT_DIR", "output")) / "latest" / "gcc_timestamp.txt"
        if p.exists():
            ts = p.read_text().strip()
    if ts:
        for fmt in ("%Y%m%d_%H%M%S", "%Y%m%d_%H%M"):
            try:
                dt = datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
                return dt, f"对齐云图 {dt:%Y-%m-%d %H:%M}Z"
            except ValueError:
                continue
        logger.warning("GCC_TIMESTAMP=%r 无法解析，改用当前时刻", ts)

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return now, f"未提供云图时间戳，用当前整点 {now:%Y-%m-%d %H:%M}Z"


def main() -> int:
    out_dir = Path(os.environ.get("OUTPUT_DIR", "output"))
    sources = config.enabled_sources()
    dt_end, why = resolve_window()

    logger.info("=" * 62)
    logger.info("闪电融合：窗口 %s .. %s（%d 分钟）",
                (dt_end - timedelta(minutes=config.WINDOW_MINUTES)).strftime("%m-%d %H:%M"),
                dt_end.strftime("%m-%d %H:%M"), config.WINDOW_MINUTES)
    logger.info("时间基准：%s", why)
    logger.info("启用源：%s   上限：%d 点   每格上限：%d",
                ",".join(sources) or "(无)", config.MAX_POINTS, config.POINTS_PER_CELL)
    if "blitzortung" in sources:
        logger.info("Blitzortung 保留区域 lon %.0f..%.0f lat %.0f..%.0f",
                    *config.BLITZ_BBOX)
    logger.info("=" * 62)

    all_pts: list[tuple[float, float, str, float]] = []
    failed: list[str] = []

    for name in sources:
        try:
            if name == "blitzortung":
                pts = blitzortung.fetch(dt_end, config.WINDOW_MINUTES)
            elif name == "glm":
                pts = goes_glm.fetch(dt_end, config.WINDOW_MINUTES)
            elif name == "eumetsat":
                pts = eumetsat_li.fetch(dt_end, config.WINDOW_MINUTES)
            else:
                pts = []
        except Exception as exc:                  # noqa: BLE001 - 单源失败不能拖垮全局
            logger.exception("源 %s 异常: %s", name, exc)
            pts = []
        if not pts:
            failed.append(name)
            logger.warning("源 %s：0 个点", name)
        else:
            logger.info("源 %s：%d 个点", name, len(pts))
        all_pts.extend(pts)

    meta_extra = {"window_end": dt_end.isoformat(), "window_minutes": config.WINDOW_MINUTES,
                  "sources_enabled": sources, "sources_empty": failed, "time_basis": why}

    if not all_pts:
        logger.warning("全部源都没有数据，回退到 %d 个硬编码坐标（既有设计行为）",
                       len(DEFAULT_LOCATIONS))
        storms = list(DEFAULT_LOCATIONS)
        meta = {"points": len(storms), "fallback": "default_locations",
                "input_points": 0, "cells": 0, "cells_used": 0}
        meta.update(meta_extra)
    else:
        storms, meta = fuse.build(all_pts)
        meta.update(meta_extra)

    fuse.write(out_dir, storms, meta)

    # 覆盖率自检：按 30°×30° 粗格统计输出点的分布，方便和改造前对比
    buckets: dict[tuple[int, int], int] = {}
    for s in storms:
        k = (int(s["lat"] // 30) * 30, int(s["lng"] // 30) * 30)
        buckets[k] = buckets.get(k, 0) + 1
    top = sorted(buckets.items(), key=lambda kv: -kv[1])[:8]
    logger.info("输出分布 top8（lat,lon 30°格 -> 点数）: %s",
                ", ".join(f"({a:+04d},{b:+04d}):{c}" for (a, b), c in top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
