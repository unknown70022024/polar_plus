"""tools/credentials.py — 在一处决定"GitHub 凭据从哪来"。

为什么单独一个模块：本机 `github.com:443` 不通，所有对 GitHub 的写操作都走 REST
API，于是好几个脚本各自读 token。凭据路径一变（本项目已经变过一次），分散的
`read_text()` 就会有心漏掉一个，而失败方式通常是 401 —— 看起来像权限问题，
实际是路径问题。

解析顺序（先命中先用）：
  1. 环境变量 ``GH_TOKEN`` / ``GITHUB_TOKEN``
     —— CI 与非交互场景用这个，凭据不落盘
  2. ``~/keys/github_pat``
     —— 当前用的位置。`~/keys` 是 700，文件 400，且**在仓库之外**，
        所以 `git add -A` 无论如何碰不到它
  3. ``~/.gh-token``
     —— 早期位置，保留作为回退

找不到就抛 ``CredentialError``，消息里列出所有找过的路径 —— 这比让 urllib
抛一个没有上下文的 401 有用得多。
"""
from __future__ import annotations

import os
import pathlib
import stat
import sys

CANDIDATES = (
    ("~/keys/token", "当前凭据：classic PAT，scope 含 repo + workflow + write:packages"),
    ("~/keys/github_pat", "回退：细粒度 PAT。仓库授权列表在创建时固定，"
                          "新建仓库不会自动纳入，容易 404"),
    ("~/.gh-token", "早期位置，回退"),
)
ENV_VARS = ("GH_TOKEN", "GITHUB_TOKEN")


class CredentialError(RuntimeError):
    """找不到可用的 GitHub 凭据。"""


def token_paths() -> list[pathlib.Path]:
    """所有会被检查的路径，按优先级排序。"""
    return [pathlib.Path(p).expanduser() for p, _ in CANDIDATES]


def find_token_path() -> pathlib.Path | None:
    """第一个存在且非空的凭据文件，或 None。"""
    for path in token_paths():
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    return None


def load_token(required: bool = True) -> str | None:
    """返回 token 字符串。

    Args:
        required: True 时找不到就抛 CredentialError；False 时返回 None。

    Raises:
        CredentialError: required=True 且既无环境变量也无凭据文件。
    """
    for var in ENV_VARS:
        value = (os.environ.get(var) or "").strip()
        if value:
            return value

    path = find_token_path()
    if path is not None:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CredentialError(f"读取 {path} 失败: {exc}") from exc
        if value:
            return value

    if not required:
        return None

    looked = "\n".join(f"    {p}  ({why})" for p, (_, why) in
                       zip(token_paths(), CANDIDATES))
    raise CredentialError(
        "找不到 GitHub 凭据。按顺序找过：\n"
        f"{looked}\n"
        f"  环境变量 {', '.join(ENV_VARS)}\n"
        "请把 PAT 放进第一个路径（文件权限 400，目录 700）。"
    )


def describe() -> str:
    """一行说明凭据来源，用于启动日志。绝不返回值本身。"""
    for var in ENV_VARS:
        if (os.environ.get(var) or "").strip():
            return f"环境变量 {var}"
    path = find_token_path()
    if path is None:
        return "（未找到）"
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mode = 0
    return f"{path}（权限 {mode:o}）"


def warn_if_permissive(path: pathlib.Path | None = None) -> str | None:
    """凭据文件权限过宽时返回一句警告，否则 None。

    `chmod 400` 是有意义的：同机其他用户读不到。`644` 意味着任何本地用户
    都能读走你的 PAT。
    """
    path = path or find_token_path()
    if path is None:
        return None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
    if mode & 0o077:
        return (f"警告: {path} 权限为 {mode:o}，同机其他用户可读。"
                f"建议 chmod 400 {path}")
    return None


if __name__ == "__main__":          # 自检：只报告来源与权限，不打印 token
    print(f"凭据来源: {describe()}")
    warn = warn_if_permissive()
    if warn:
        print(warn)
        sys.exit(1)
    try:
        load_token()
        print("读取成功")
    except CredentialError as exc:
        print(exc)
        sys.exit(2)
