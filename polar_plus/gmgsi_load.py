"""
polar_plus/gmgsi_load.py — GMGSI 数据加载（无对比度拉伸，无极区填充）

从 AWS S3 读取 GMGSI NetCDF 文件，输出云密度数组。
"""
import os
import re
import logging
from datetime import datetime, timezone, timedelta
import numpy as np

from polar_plus.config import (GMGSI_BUCKET, GMGSI_REGION,
                                BT_WARM, BT_COLD, BT_FILL)

logger = logging.getLogger(__name__)


def _get_s3_client():
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    return boto3.client(
        "s3",
        config=Config(signature_version=UNSIGNED),
        region_name=GMGSI_REGION,
    )


def find_latest_gmgsi(max_days_back: int = 14) -> tuple:
    """S3 查找最新 GMGSI 文件"""
    s3 = _get_s3_client()
    now = datetime.now(timezone.utc)
    start_hour = max(0, now.hour - 3)
    for days_ago in range(max_days_back + 1):
        d = now - timedelta(days=days_ago)
        start_h = start_hour if days_ago == 0 else 23
        for hour in range(start_h, -1, -1):
            prefix = f"GMGSI_LW/{d.year}/{d.month:02d}/{d.day:02d}/{hour:02d}/"
            try:
                resp = s3.list_objects_v2(Bucket=GMGSI_BUCKET, Prefix=prefix, MaxKeys=10)
            except Exception:
                continue
            nc_files = [
                (obj["Key"], obj["Size"], obj["LastModified"])
                for obj in resp.get("Contents", [])
                if obj["Key"].endswith(".nc") and "_blend_" in obj["Key"]
            ]
            if nc_files:
                nc_files.sort(key=lambda x: x[1], reverse=True)
                key, size, lastmod = nc_files[0]
                match = re.search(r"_s(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", key)
                if match:
                    y, mo, d, hh, mm, ss = match.groups()
                    dt = datetime(int(y), int(mo), int(d),
                                  int(hh), int(mm), int(ss), tzinfo=timezone.utc)
                else:
                    dt = lastmod
                return (dt, key, size)
    raise FileNotFoundError(f"No GMGSI data found in past {max_days_back} days")


def read_gmgsi_netcdf(nc_path: str) -> tuple:
    """读取 GMGSI NetCDF"""
    import xarray as xr
    ds = xr.open_dataset(nc_path)
    data_var = None
    for vn in ['data', 'LIR', 'IR', 'IRWIN']:
        if vn in ds.data_vars:
            data_var = vn
            break
    if data_var is None:
        data_var = list(ds.data_vars.keys())[0]
    vals = ds[data_var].values.squeeze()
    lat = ds['lat'].values
    timestamp_str = ds.attrs.get('time_coverage_start', '')
    if timestamp_str:
        ts = timestamp_str.replace('T', '_').replace(':', '').replace('Z', '').replace('-', '')
        timestamp_str = ts[:15]
    data_attrs = ds[data_var].attrs
    is_pre_scaled = '0-255' in str(data_attrs.get('long_name', ''))
    ds.close()
    logger.info(f"GMGSI: shape={vals.shape}, dtype={vals.dtype}, lat={float(lat.min()):.1f}~{float(lat.max()):.1f}")
    return vals, lat, is_pre_scaled, timestamp_str


def _brightness_temp_to_density(data: np.ndarray) -> np.ndarray:
    """Kelvin -> 云密度 0-255 uint8"""
    arr = data.astype(np.float32)
    mask = (arr <= BT_FILL + 1) | np.isnan(arr)
    density = (BT_WARM - arr) / (BT_WARM - BT_COLD)
    density = np.clip(density, 0.0, 1.0)
    density[mask] = 0.0
    return (density * 255).astype(np.uint8)


def bt_to_density(vals: np.ndarray, is_pre_scaled: bool) -> np.ndarray:
    """BT 转密度（无对比度拉伸）"""
    if is_pre_scaled:
        return np.clip(vals, 0, 255).astype(np.uint8)
    return _brightness_temp_to_density(vals)


def post_process_density(density: np.ndarray,
                         threshold: int = 35) -> np.ndarray:
    """
    Remove low-density noise and apply linear contrast stretch.

    threshold: pixel values below this become 0 (clear sky).
    Linear stretch: [threshold, max] → [0, 255] preserves cloud feature
    geometry without the centroid shift that gamma correction causes.
    """
    # 1. Threshold — kill atmospheric noise in clear areas
    density = np.where(density < threshold, 0, density)

    # 2. Linear contrast stretch — preserves relative brightness ordering
    valid = density[density > 0]
    if len(valid) > 1:
        vmin, vmax = valid.min(), valid.max()
        if vmax > vmin:
            density = np.clip(
                (density.astype(np.float32) - vmin) / (vmax - vmin) * 255.0,
                0, 255
            ).astype(np.uint8)

    logger.info(
        f"Post-process: threshold={threshold}, linear stretch, "
        f"zeros={np.sum(density == 0) / density.size * 100:.1f}%"
    )
    return density


def load_core_density(target_w: int = 5000, max_days_back: int = 14) -> tuple:
    """
    主入口：查找最新 GMGSI -> 下载 -> BT转密度 -> padding 至 90N-90S

    返回:
        density: (target_h, target_w) uint8
        lat_grid: (target_h,) 纬度
        lon_grid: (target_w,) 经度
        timestamp_str: 时间戳
    """
    import tempfile
    dt, s3_key, size = find_latest_gmgsi(max_days_back)
    timestamp_str = dt.strftime("%Y%m%d_%H%M%S")
    logger.info(f"Latest GMGSI: {s3_key} ({size/1024/1024:.1f}MB)")

    tmp_path = tempfile.mktemp(suffix='.nc')
    try:
        s3 = _get_s3_client()
        s3.download_file(GMGSI_BUCKET, s3_key, tmp_path)
        logger.info(f"Downloaded: {os.path.getsize(tmp_path)/1024/1024:.1f} MB")

        vals, lat, is_pre_scaled, _ = read_gmgsi_netcdf(tmp_path)
        density = bt_to_density(vals, is_pre_scaled)

        # padding 到等距柱面 90N-90S
        src_h, src_w = density.shape
        span_src = float(lat.max() - lat.min())
        target_h = int(round(src_h * 180.0 / span_src))
        padding_top = int(round((90.0 - float(lat.max())) / 180.0 * target_h))

        padded = np.zeros((target_h, target_w), dtype=np.uint8)
        padded[padding_top:padding_top + src_h, 0:src_w] = density

        lat_grid = np.linspace(90.0, -90.0, target_h)
        lon_grid = np.linspace(-180.0, 180.0, target_w)

        logger.info(f"Core density: {padded.shape}, zeros={np.sum(padded==0)/padded.size*100:.1f}%")
        return padded, lat_grid, lon_grid, timestamp_str
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
