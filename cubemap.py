"""
polar_plus/cubemap.py — equirectangular -> 6 面 cubemap 投影
"""
import math
import numpy as np
from PIL import Image
from polar_plus.config import FACES


def face_direction(face_idx: int, u: float, v: float):
    if face_idx == 0:
        d = (1.0, -v, -u)
    elif face_idx == 1:
        d = (-1.0, -v, u)
    elif face_idx == 2:
        d = (u, 1.0, v)
    elif face_idx == 3:
        d = (u, -1.0, -v)
    elif face_idx == 4:
        d = (u, -v, 1.0)
    elif face_idx == 5:
        d = (-u, -v, -1.0)
    else:
        raise ValueError(f"Invalid face index: {face_idx}")
    length = math.sqrt(d[0]**2 + d[1]**2 + d[2]**2)
    if length < 1e-10:
        return (0.0, 0.0, 1.0)
    return (d[0] / length, d[1] / length, d[2] / length)


def direction_to_equirect(dx: float, dy: float, dz: float,
                          src_w: int, src_h: int,
                          lon_offset_deg: float = 0.0):
    lat = math.asin(max(-1.0, min(1.0, dy)))
    lon = math.atan2(-dx, dz) + math.radians(lon_offset_deg)
    ix = (lon / (2 * math.pi) + 0.5) * src_w
    iy = (0.5 - lat / math.pi) * src_h
    ix = ix % src_w
    iy = max(0.0, min(float(src_h - 1), iy))
    return ix, iy


def sample_bilinear(data: np.ndarray, x: float, y: float):
    h, w = data.shape[:2]
    x0 = int(x)
    y0 = int(y)
    x1 = (x0 + 1) % w
    y1 = min(y0 + 1, h - 1)
    fx = x - x0
    fy = y - y0
    v00 = float(data[y0, x0])
    v10 = float(data[y0, x1])
    v01 = float(data[y1, x0])
    v11 = float(data[y1, x1])
    result = (v00 * (1 - fx) * (1 - fy) +
              v10 * fx * (1 - fy) +
              v01 * (1 - fx) * fy +
              v11 * fx * fy)
    return max(0, min(255, int(round(result))))


def equirect_to_cubemap(src_data: np.ndarray,
                        src_w: int, src_h: int,
                        face_size: int = 1024,
                        lon_offset: float = 0.0) -> dict:
    results = {}
    for fi, face_name in enumerate(FACES):
        out = np.zeros((face_size, face_size), dtype=np.uint8)
        for fy in range(face_size):
            v = (fy / face_size) * 2.0 - 1.0
            for fx in range(face_size):
                u = (fx / face_size) * 2.0 - 1.0
                dx, dy, dz = face_direction(fi, u, v)
                ix, iy = direction_to_equirect(dx, dy, dz, src_w, src_h, lon_offset)
                if iy < 0 or iy >= src_h:
                    out[fy, fx] = 0
                else:
                    out[fy, fx] = sample_bilinear(src_data, ix, iy)
        results[face_name] = Image.fromarray(out, mode='L')
        print(f"  OK [{face_name}] face generated ({face_size}x{face_size})")
    return results
