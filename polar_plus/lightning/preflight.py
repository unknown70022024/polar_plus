"""
light.preflight — 逐项体检，不跑完整管线。

    python -m light.preflight

每一项独立报告，便于定位是哪个源没配好。
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

from polar_plus.lightning import config
from polar_plus.lightning.sources import blitzortung, eumetsat_li, goes_glm

logging.basicConfig(level=logging.WARNING, format="    %(message)s")

OK, BAD, SKIP = "OK  ", "FAIL", "SKIP"


def _line(tag: str, name: str, detail: str = "") -> None:
    print(f"  [{tag}] {name}" + (f"  —— {detail}" if detail else ""))


def main() -> int:
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(minutes=10)      # 用 10 分钟窗口快速体检
    failures = 0

    print("=" * 66)
    print("环境")
    print("=" * 66)
    _line(OK, "LIGHT_SOURCES", ",".join(config.enabled_sources()))
    _line(OK, "时间窗", f"{start:%m-%d %H:%M} .. {end:%m-%d %H:%M} UTC（10 分钟）")
    _line(OK, "输出上限", f"{config.MAX_POINTS} 点, 每格 {config.POINTS_PER_CELL}")

    print()
    print("=" * 66)
    print("1/3  Blitzortung")
    print("=" * 66)
    pts = blitzortung.fetch(end, 10)
    if pts:
        lons = [p[1] for p in pts]
        lats = [p[0] for p in pts]
        _line(OK, "拉取成功", f"{len(pts)} 点, lon {min(lons):.0f}..{max(lons):.0f}, "
                              f"lat {min(lats):.0f}..{max(lats):.0f}")
        if not (70 <= min(lons) and max(lons) <= 180):
            _line(BAD, "亚太过滤异常", "经度超出 70..180")
            failures += 1
    else:
        _line(BAD, "拉取失败", "检查网络或 BO_SERVICE_URL")
        failures += 1

    print()
    print("=" * 66)
    print("2/3  NOAA GOES GLM")
    print("=" * 66)
    pts = goes_glm.fetch(end, 10)
    if pts:
        by = {}
        for _, _, s, _ in pts:
            by[s] = by.get(s, 0) + 1
        _line(OK, "拉取成功", f"{len(pts)} 闪击, 分星 {by}")
    else:
        _line(BAD, "拉取失败", "检查 S3 可达性")
        failures += 1

    print()
    print("=" * 66)
    print("3/3  EUMETSAT MTG LI")
    print("=" * 66)
    if not config.eumetsat_enabled():
        _line(SKIP, "未配置密钥", "把 EUMETSAT_CONSUMER_KEY/SECRET 写进 "
                                  "~/.lightning.env 或 .env")
    else:
        _line(OK, "密钥已配置", f"key 尾部 …{config.EUMETSAT_KEY[-4:]}")
        token = eumetsat_li.get_token()
        if not token:
            _line(BAD, "换取 token 失败", "检查 key/secret 是否正确")
            failures += 1
        else:
            _line(OK, "换取 token 成功", f"长度 {len(token)}")
            entries = eumetsat_li.search(start, end, token)
            if entries:
                _line(OK, "检索成功", f"{len(entries)} 条")
            else:
                _line(BAD, "检索失败", "OpenSearch 无返回")
                failures += 1
            pts = eumetsat_li.fetch(end, 10)
            if pts:
                lons = [p[1] for p in pts]
                _line(OK, "下载+解析成功", f"{len(pts)} 闪击, "
                                           f"lon {min(lons):.0f}..{max(lons):.0f}")
            else:
                _line(BAD, "下载或解析失败",
                      "若检索正常，多半是下载授权或 NetCDF 变量名问题，"
                      "看上面的 warning 日志")
                failures += 1

    print()
    print("=" * 66)
    if failures:
        print(f"{failures} 项失败")
    else:
        print("全部通过 —— 可以跑完整管线了：python -m light.pipeline")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
