"""
light.fuse — 把三个源的点集融合成 storms.json。

为什么不是全局随机抽样
----------------------
现有实现用 random.choices 按雷击次数加权抽样。欧洲一个 30°×30° 格子就占
全球 57%，亚太只有 1.6% —— 随机抽样会让亚太几乎抽不到点。

这里改成**网格化 + 每格上限**：

  1. 所有点归入 GRID_DEG × GRID_DEG 的格子
  2. 每个格子最多保留 POINTS_PER_CELL 个点
  3. 格子按活跃度（权重和）降序，依次取点直到 MAX_POINTS

因为每格上限相同，欧洲再密也只能占满自己的格子数，不会淹没亚太。
这保证的是**地理代表性**，而不是"按雷击次数还原真实密度"——对一个
"哪里有雷暴"的可视化来说，前者才是要的东西。

输出契约
--------
storms.json 必须是**裸数组** [{"lat":..,"lng":..}]，因为 App 侧
StormProtos.fromJson 用的是 new JSONArray(...)。诊断信息写到
storms_meta.json，不污染主文件。
"""
from __future__ import annotations

import json
import logging
import math
import random
from collections import defaultdict
from pathlib import Path

from polar_plus.lightning import config

logger = logging.getLogger(__name__)

Point = tuple[float, float, str, float]      # lat, lon, source, weight


def _cell_key(lat: float, lon: float, grid: float) -> tuple[int, int]:
    return (int(math.floor(lat / grid)), int(math.floor(lon / grid)))


def build(points: list[Point],
          max_points: int | None = None,
          grid_deg: float | None = None,
          per_cell: int | None = None) -> tuple[list[dict], dict]:
    """融合点集 -> (裸数组, 诊断元数据)。"""
    max_points = max_points or config.MAX_POINTS
    grid_deg = grid_deg or config.GRID_DEG
    per_cell = per_cell or config.POINTS_PER_CELL

    cells: dict[tuple[int, int], list[Point]] = defaultdict(list)
    for p in points:
        cells[_cell_key(p[0], p[1], grid_deg)].append(p)

    by_source: dict[str, int] = defaultdict(int)
    for p in points:
        by_source[p[2]] += 1

    # 每个格子的代表点：按权重取前 per_cell 个（权重相同则随机）
    scored = []
    for key, pts in cells.items():
        score = sum(p[3] for p in pts)
        if len(pts) > per_cell:
            # 权重高的优先，权重并列时随机打散，避免总是取到同一批
            pool = sorted(pts, key=lambda p: (-p[3], random.random()))
            chosen = pool[:per_cell]
        else:
            chosen = list(pts)
        scored.append((score, key, chosen))

    # 活跃格子优先
    scored.sort(key=lambda t: (-t[0], t[1]))

    out: list[dict] = []
    cells_used = 0
    for _score, _key, chosen in scored:
        if len(out) >= max_points:
            break
        cells_used += 1
        for lat, lon, _src, _w in chosen:
            if len(out) >= max_points:
                break
            out.append({"lat": round(lat, 4), "lng": round(lon, 4)})

    meta = {
        "points": len(out),
        "input_points": len(points),
        "cells": len(cells),
        "cells_used": cells_used,
        "grid_deg": grid_deg,
        "points_per_cell": per_cell,
        "by_source": dict(sorted(by_source.items())),
        "blitz_bbox": list(config.BLITZ_BBOX),
        "max_points": max_points,
    }
    logger.info("融合: %d 个输入点 -> %d 格 -> 输出 %d 点 (%d 格被用到)",
                len(points), len(cells), len(out), cells_used)
    logger.info("融合: 各源输入点数 %s", meta["by_source"])
    return out, meta


def write(out_dir: Path, storms: list[dict], meta: dict) -> tuple[Path, Path]:
    """写 storms.json（裸数组）+ storms_meta.json。"""
    latest = out_dir / "latest"
    latest.mkdir(parents=True, exist_ok=True)

    p_main = latest / "storms.json"
    p_main.write_text(json.dumps(storms, separators=(",", ":")))

    p_meta = latest / "storms_meta.json"
    p_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=1))

    logger.info("写入 %s (%d 点, %d 字节)", p_main, len(storms), p_main.stat().st_size)
    logger.info("写入 %s", p_meta)
    return p_main, p_meta
