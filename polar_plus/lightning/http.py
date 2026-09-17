"""
light.http — 带**总时长**截止的 HTTP 读取。

为什么需要这个
--------------
`urllib.request.urlopen(timeout=N)` 的 N 是**单次 socket 操作**的超时，不是整个
请求的总时长。一个慢慢滴流的连接可以每次都刚好不触发超时，从而无限期挂住。

实测：60 分钟窗口拉 359 个 GLM 文件，前 350 个 3 分钟就下完了，最后 9 个卡了
5 分钟以上（远超 90 秒的 timeout），整个管线被拖死。

所以这里按块读，并在每个块之间检查**墙钟截止时间**，到点就放弃。
丢几个文件对一个可视化产品无所谓；挂住整个管线不行。
"""
from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_CHUNK = 64 * 1024


def fetch_bytes(url: str, headers: dict | None = None,
                timeout: float = 30.0) -> bytes | None:
    """下载一个 URL，总时长超过 timeout 秒就放弃。失败返回 None。"""
    deadline = time.monotonic() + timeout
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            parts: list[bytes] = []
            while True:
                # 每次 read 最多阻塞 timeout 秒（socket 超时），块之间检查墙钟。
                # 所以总时长上界是 timeout + timeout。
                block = resp.read(_CHUNK)
                if not block:
                    break
                parts.append(block)
                if time.monotonic() > deadline:
                    logger.debug("下载超时（总时长 %.0fs，已收 %d 字节）: %s",
                                 timeout, sum(len(p) for p in parts), url[-70:])
                    return None
            return b"".join(parts)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError,
            TimeoutError, ValueError) as exc:
        logger.debug("下载失败 %s: %s: %s", url[-70:], type(exc).__name__, exc)
        return None


def fetch_text(url: str, headers: dict | None = None, timeout: float = 30.0
               ) -> str | None:
    blob = fetch_bytes(url, headers=headers, timeout=timeout)
    if blob is None:
        return None
    return blob.decode("utf-8", "replace")
