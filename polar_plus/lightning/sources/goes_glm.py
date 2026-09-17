"""
light.sources.goes_glm — NOAA GOES-R 静止轨道闪电成像仪（GLM）L2 LCFA。

数据在 AWS S3 上，**匿名可读，不需要任何账号**：
    https://noaa-goes19.s3.amazonaws.com/GLM-L2-LCFA/YYYY/DDD/HH/OR_GLM-L2-LCFA_G19_s..._e..._c....nc
    https://noaa-goes18.s3.amazonaws.com/GLM-L2-LCFA/YYYY/DDD/HH/OR_GLM-L2-LCFA_G18_s..._e..._c....nc

实测（2026-09-17）：
  * 每个文件覆盖 20 秒，约 180 个/小时/星
  * 文件体积 G19 约 380 KB、G18 约 230 KB
  * 窗口结束到上架约 10-30 秒
  * 变量 flash_lat / flash_lon / flash_quality_flag 直接可用

文件名里的时间戳：sYYYYDDDHHMMSS0 / eYYYYDDDHHMMSS0 / cYYYYDDDHHMMSS0
（4 位年 + 3 位年积日 + 时分秒 + 十分之一秒）
"""
from __future__ import annotations

import concurrent.futures
import io
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone

from polar_plus.lightning import config, http

logger = logging.getLogger(__name__)

_S3_LIST = "https://{bucket}.s3.amazonaws.com/"
_S3_GET = "https://{bucket}.s3.amazonaws.com/{key}"


def _parse_name_time(tag: str) -> datetime | None:
    """把 'YYYYDDDHHMMSS0' 解析成 UTC datetime。"""
    if len(tag) < 14 or not tag[:14].isdigit():
        return None
    year = int(tag[0:4])
    doy = int(tag[4:7])
    hh, mm, ss = int(tag[7:9]), int(tag[9:11]), int(tag[11:13])
    tenths = int(tag[13])
    try:
        base = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1)
        return base.replace(hour=hh, minute=mm, second=ss) + timedelta(
            seconds=tenths / 10.0)
    except ValueError:
        return None


def _file_window(key: str) -> tuple[datetime, datetime] | None:
    name = key.rsplit("/", 1)[-1]
    s = e = None
    for part in name.split("_"):
        if part.startswith("s") and len(part) >= 15:
            s = _parse_name_time(part[1:])
        elif part.startswith("e") and len(part) >= 15:
            e = _parse_name_time(part[1:])
    if s and e:
        return s, e
    return None


def _list_hour(bucket: str, dt: datetime) -> list[str]:
    """列出某小时目录下所有 GLM-L2-LCFA 文件 key（处理分页）。"""
    prefix = "%s/%04d/%03d/%02d/" % (config.GLM_PREFIX, dt.year,
                                     dt.timetuple().tm_yday, dt.hour)
    keys: list[str] = []
    token = None
    while True:
        q = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token:
            q["continuation-token"] = token
        url = _S3_LIST.format(bucket=bucket) + "?" + urllib.parse.urlencode(q)
        body = http.fetch_bytes(url, timeout=60)
        if body is None:
            logger.warning("GLM 列目录失败 %s", prefix)
            return keys
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            logger.warning("GLM 列目录返回非 XML (%s): %s", prefix, exc)
            return keys
        ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
        for node in root.findall(f"{ns}Contents"):
            k = node.find(f"{ns}Key")
            if k is not None and k.text:
                keys.append(k.text)
        truncated = (root.findtext(f"{ns}IsTruncated") or "false").lower() == "true"
        token = root.findtext(f"{ns}NextContinuationToken")
        if not truncated or not token:
            break
    return keys


def _download_flashes(bucket: str, key: str, t_start: datetime,
                      t_end: datetime) -> list[tuple[float, float, str, float]]:
    """下载并解析一个 GLM 文件 → 闪击点。"""
    import h5py
    import numpy as np

    url = _S3_GET.format(bucket=bucket, key=urllib.parse.quote(key))
    blob = http.fetch_bytes(url, timeout=config.GLM_TIMEOUT)
    if blob is None:
        return []

    tag = "goes18" if "G18" in key else "goes19"
    try:
        with h5py.File(io.BytesIO(blob), "r") as h:
            lat = np.asarray(h["flash_lat"][:], dtype="float64")
            lon = np.asarray(h["flash_lon"][:], dtype="float64")
            q = np.asarray(h["flash_quality_flag"][:], dtype="int16")
    except Exception as exc:                      # noqa: BLE001 - 单个文件坏不该整批失败
        logger.debug("GLM 解析失败 %s: %s: %s", key, type(exc).__name__, exc)
        return []

    if lat.size == 0:
        return []
    ok = (q == 0) & np.isfinite(lat) & np.isfinite(lon) & (lon > -999)
    lat, lon = lat[ok], lon[ok]
    return [(float(a), float(b), tag, 1.0) for a, b in zip(lat, lon)]


def fetch(dt_end: datetime, window_minutes: int,
          sats: tuple[str, ...] = ("goes18", "goes19")
          ) -> list[tuple[float, float, str, float]]:
    """取窗口内的 GOES GLM 闪击。

    Args:
        dt_end: 窗口结束（UTC）。
        window_minutes: 窗口长度。
        sats: 要取的卫星，默认两颗都取。

    Returns:
        [(lat, lon, "goes18"/"goes19", 1.0), ...]；失败时返回空列表。
    """
    if dt_end.tzinfo is None:
        dt_end = dt_end.replace(tzinfo=timezone.utc)
    t_end = dt_end
    t_start = t_end - timedelta(minutes=window_minutes)

    # 收集覆盖窗口的所有小时目录
    hours: list[datetime] = []
    cur = t_start.replace(minute=0, second=0, microsecond=0)
    while cur <= t_end:
        hours.append(cur)
        cur += timedelta(hours=1)

    jobs: list[tuple[str, str]] = []
    for sat in sats:
        bucket = config.GOES_BUCKETS.get(sat)
        if not bucket:
            continue
        for h in hours:
            for key in _list_hour(bucket, h):
                fw = _file_window(key)
                if fw is None:
                    continue
                fs, fe = fw
                if fs < t_end and fe > t_start:      # 有交集
                    jobs.append((bucket, key))

    if not jobs:
        logger.warning("GOES GLM: 窗口 %s .. %s 内没有文件",
                       t_start.isoformat(), t_end.isoformat())
        return []
    if config.GLM_FILE_STRIDE > 1:
        before = len(jobs)
        jobs = jobs[::config.GLM_FILE_STRIDE]
        logger.info("GOES GLM: 抽样步长 %d，%d -> %d 个文件",
                    config.GLM_FILE_STRIDE, before, len(jobs))

    if len(jobs) > config.GLM_MAX_FILES:
        logger.warning("GOES GLM: 文件数 %d 超过上限 %d，截断",
                       len(jobs), config.GLM_MAX_FILES)
        jobs = jobs[:config.GLM_MAX_FILES]

    logger.info("GOES GLM: 窗口 %s .. %s，%d 个文件，并发 %d",
                t_start.strftime("%m-%d %H:%M"), t_end.strftime("%m-%d %H:%M"),
                len(jobs), config.GLM_CONCURRENCY)

    pts: list[tuple[float, float, str, float]] = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=config.GLM_CONCURRENCY) as pool:
        futs = {pool.submit(_download_flashes, b, k, t_start, t_end): (b, k)
                for b, k in jobs}
        for fut in concurrent.futures.as_completed(futs):
            done += 1
            try:
                pts.extend(fut.result())
            except Exception as exc:              # noqa: BLE001
                logger.debug("GLM 任务异常: %s", exc)
            if done % 50 == 0:
                logger.info("  GLM 进度 %d/%d，已收集 %d 个闪击",
                            done, len(jobs), len(pts))

    logger.info("GOES GLM: %d 个文件 -> %d 个闪击点", len(jobs), len(pts))
    return pts
