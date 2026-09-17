"""
polar_plus/cubemap.py — equirectangular -> 6 面 cubemap 投影

The three scalar helpers below (``face_direction``, ``direction_to_equirect``,
``sample_bilinear``) are the NORMATIVE definition of the mapping — they are the
original implementation and the reference the published tiles were produced
from. ``equirect_to_cubemap`` is a vectorised composition of exactly those
three functions; if you change the maths, change it there too or the two will
drift.

Why vectorised: the scalar version is a 6 x 1024 x 1024 = 6.3M-iteration
Python loop with a sqrt, an asin and an atan2 per pixel. Measured 12.6s on a
16-core dev box and ~25s in the 2-vCPU container — 5% of a full run, all of it
interpreter overhead. The vectorised path does the identical arithmetic with
numpy.
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
    lon = math.atan2(dx, dz) + math.radians(lon_offset_deg)
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


def _face_components(face_idx: int, u: np.ndarray, v: np.ndarray):
    """Vectorised ``face_direction``: the three NORMALISED components.

    Same branch table and same order of operations as the scalar version, so
    the floats are identical — every operation involved (multiply, add,
    divide, sqrt) is IEEE-754 correctly rounded, so vectorising cannot change
    the result.
    """
    one = np.ones_like(u)
    if face_idx == 0:
        d0, d1, d2 = one, -v, -u
    elif face_idx == 1:
        d0, d1, d2 = -one, -v, u
    elif face_idx == 2:
        d0, d1, d2 = u, one, v
    elif face_idx == 3:
        d0, d1, d2 = u, -one, -v
    elif face_idx == 4:
        d0, d1, d2 = u, -v, one
    elif face_idx == 5:
        d0, d1, d2 = -u, -v, -one
    else:
        raise ValueError(f"Invalid face index: {face_idx}")

    length = np.sqrt(d0**2 + d1**2 + d2**2)
    # Scalar version substitutes (0, 0, 1) for a degenerate direction. On this
    # grid the length is always >= 1 (one component is exactly +/-1.0), so the
    # branch is unreachable; kept so the two implementations stay equivalent.
    degenerate = length < 1e-10
    safe = np.where(degenerate, 1.0, length)
    nd0 = np.where(degenerate, 0.0, d0 / safe)
    nd1 = np.where(degenerate, 0.0, d1 / safe)
    nd2 = np.where(degenerate, 1.0, d2 / safe)
    return nd0, nd1, nd2


def equirect_to_cubemap(src_data: np.ndarray,
                        src_w: int,
                        src_h: int,
                        face_size: int = 1024,
                        lon_offset: float = 0.0) -> dict:
    """Project an equirectangular image onto the six cube faces.

    Per pixel this is exactly:

        dx, dy, dz = face_direction(fi, u, v)
        ix, iy     = direction_to_equirect(dy, dz, dx, src_w, src_h, lon_offset)
        out        = sample_bilinear(src_data, ix, iy)

    Note the argument shuffle into ``direction_to_equirect``: it receives
    (dy, dz, dx), so inside it ``lat = asin(dz)`` and ``lon = atan2(dy, dx)``
    of the *face* direction. Getting that wrong silently rotates the sky, so
    the composition is spelled out rather than "tidied up".
    """
    src = np.asarray(src_data)
    h, w = src.shape[:2]

    # u varies along a row, v along a column.
    axis = (np.arange(face_size, dtype=np.float64) / face_size) * 2.0 - 1.0
    u = np.broadcast_to(axis.reshape(1, face_size), (face_size, face_size))
    v = np.broadcast_to(axis.reshape(face_size, 1), (face_size, face_size))

    two_pi = 2 * math.pi
    lon_offset_rad = math.radians(lon_offset)
    max_iy = float(src_h - 1)

    results = {}
    for fi, face_name in enumerate(FACES):
        dx, dy, dz = _face_components(fi, u, v)

        # direction_to_equirect(dy, dz, dx, ...) — first positional arg is the
        # asin input, so `lat` takes dz and `lon` takes (dy, dx).
        lat = np.arcsin(np.clip(dz, -1.0, 1.0))
        lon = np.arctan2(dy, dx) + lon_offset_rad
        ix = (lon / two_pi + 0.5) * src_w
        iy = (0.5 - lat / math.pi) * src_h
        ix = np.mod(ix, src_w)
        # iy is clamped to [0, src_h-1] by direction_to_equirect, so the scalar
        # version's `if iy < 0 or iy >= src_h: out = 0` branch is unreachable.
        iy = np.clip(iy, 0.0, max_iy)

        x0 = np.floor(ix).astype(np.intp)
        y0 = np.floor(iy).astype(np.intp)
        x1 = (x0 + 1) % w
        y1 = np.minimum(y0 + 1, h - 1)
        fx = ix - x0
        fy = iy - y0

        v00 = src[y0, x0].astype(np.float64)
        v10 = src[y0, x1].astype(np.float64)
        v01 = src[y1, x0].astype(np.float64)
        v11 = src[y1, x1].astype(np.float64)

        out = (v00 * (1 - fx) * (1 - fy) +
               v10 * fx * (1 - fy) +
               v01 * (1 - fx) * fy +
               v11 * fx * fy)
        # Mirrors max(0, min(255, int(round(result)))): np.rint and Python's
        # round() are both round-half-to-even.
        out = np.clip(np.rint(out), 0, 255).astype(np.uint8)

        results[face_name] = Image.fromarray(out, mode='L')
        print(f"  OK [{face_name}] face generated ({face_size}x{face_size})")
    return results
