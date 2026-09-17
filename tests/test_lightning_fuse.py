"""lightning 融合与覆盖模型的单元测试。

直接跑：python tests/test_lightning_fuse.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polar_plus.lightning import config, fuse  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


print("== 覆盖模型（用实测视场）==")
check("GOES-West 覆盖 170E", config.satellite_for(0, 170) == "goes18")
check("GOES-West 覆盖日界线以西", config.satellite_for(0, -170) == "goes18")
# -80W 落在两颗星的视场重叠区（G18 到 72.8W，G19 从 139.4W 起），返回哪颗都对
check("GOES 覆盖 -80W（重叠区）", config.satellite_for(10, -80) in ("goes18", "goes19"))
check("GOES-East 独有区 -30W", config.satellite_for(10, -30) == "goes19")
check("MTG LI 覆盖 0 度", config.satellite_for(45, 5) == "mtg-li")
check("高纬超出 GLM", config.satellite_for(70, -100) is None)
check("亚太缺口无卫星 (-100E=260E 不成立)", config.satellite_for(20, 120) is None,
      str(config.satellite_for(20, 120)))
check("亚太缺口无卫星 (印度)", config.satellite_for(20, 78) is None,
      str(config.satellite_for(20, 78)))

print("\n== Blitzortung 亚太框 ==")
check("上海在内", config.blitz_in_bbox(31.2, 121.5))
check("东京在内", config.blitz_in_bbox(35.7, 139.7))
check("悉尼在内", config.blitz_in_bbox(-33.9, 151.2))
check("伦敦在外", not config.blitz_in_bbox(51.5, -0.1))
check("纽约在外", not config.blitz_in_bbox(40.7, -74.0))
check("开普敦在外", not config.blitz_in_bbox(-33.9, 18.4))

print("\n== 融合：每格上限保证地理代表性 ==")
# 欧洲 1 格塞 500 个点，亚太 1 格只有 1 个点
pts = [(50.0, 10.0, "x", 1.0)] * 500 + [(30.0, 120.0, "y", 1.0)]
out, meta = fuse.build(pts, max_points=100, grid_deg=1.0, per_cell=3)
eu = [p for p in out if 40 < p["lat"] < 60 and 0 < p["lng"] < 20]
ap = [p for p in out if 20 < p["lat"] < 40 and 110 < p["lng"] < 130]
check("欧洲格被压到 3 个点", len(eu) == 3, f"got {len(eu)}")
check("亚太格的点不会被挤掉", len(ap) == 1, f"got {len(ap)}")
check("总点数不超上限", len(out) <= 100, f"got {len(out)}")

print("\n== 融合：输出上限 ==")
pts2 = [(float(i % 60 - 30), float(i % 300 - 150), "x", 1.0) for i in range(5000)]
out2, meta2 = fuse.build(pts2, max_points=200, grid_deg=1.0, per_cell=3)
check("截断到 max_points", len(out2) <= 200, f"got {len(out2)}")

print("\n== 输出契约（App 依赖）==")
check("是 list", isinstance(out2, list))
check("元素只有 lat/lng", all(set(p.keys()) == {"lat", "lng"} for p in out2[:50]))
check("坐标是数值", all(isinstance(p["lat"], float) and isinstance(p["lng"], float)
                        for p in out2[:50]))
check("纬度在范围内", all(-90 <= p["lat"] <= 90 for p in out2))
check("经度在范围内", all(-180 <= p["lng"] <= 180 for p in out2))

print("\n== meta ==")
check("有 by_source", "by_source" in meta2)
check("有 grid_deg", "grid_deg" in meta2)  # window 字段由 pipeline 补

print()
if fails:
    print(f"{len(fails)} 个失败: {fails}")
    sys.exit(1)
print("全部通过")
