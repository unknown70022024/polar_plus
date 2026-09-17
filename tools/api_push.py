#!/usr/bin/env python3
"""增量推送 polar_plus 到 GitHub Git Data API（带本地↔远端 sha 映射）。

为什么需要映射表
----------------
本机 github.com:443 不通，只能走 REST API 建提交。GitHub 会重新计算 sha，
所以远端提交和本地提交**内容相同但 sha 不同**。于是：

  * `git rev-list <远端头>..HEAD` 无法使用（远端头在本地不存在）
  * 每次推送都会把整段历史重建一遍，历史越推越慢

映射表存在 .git/polar-remote-map.json，记录「本地 sha -> 远端 sha」。
下次推送时用远端头反查出对应的本地提交，只重放它之后的增量。

用法:
    python tools/api_push.py [owner/repo] [branch]
"""
import base64
import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

TOKEN = pathlib.Path.home().joinpath(".gh-token").read_text().strip()
REPO = sys.argv[1] if len(sys.argv) > 1 else "unknown70022024/polar_plus"
BRANCH = sys.argv[2] if len(sys.argv) > 2 else "main"
API = "https://api.github.com"

MAP_PATH = subprocess.run(["git", "rev-parse", "--git-dir"],
                          capture_output=True, text=True,
                          check=True).stdout.strip()
MAP_FILE = pathlib.Path(MAP_PATH) / "polar-remote-map.json"


def api(method, path, data=None, quiet=False):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        API + path, method=method, data=body,
        headers={"Authorization": "token " + TOKEN,
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:400].decode("utf-8", "replace")
        if not quiet:
            print("  !! %s %s -> HTTP %s: %s" % (method, path, exc.code, detail))
        raise


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          check=True).stdout


def load_map():
    if MAP_FILE.is_file():
        try:
            return json.loads(MAP_FILE.read_text())
        except Exception:                          # noqa: BLE001
            pass
    return {}


def save_map(m):
    MAP_FILE.write_text(json.dumps(m, indent=1, sort_keys=True))


def main():
    mapping = load_map()
    remote_to_local = {v: k for k, v in mapping.items()}

    try:
        head = api("GET", "/repos/%s/git/ref/heads/%s" % (REPO, BRANCH),
                   quiet=True)["object"]["sha"]
    except urllib.error.HTTPError:
        head = None
        print("远端 %s 不存在，将新建" % BRANCH)

    if head is None:
        local_base = None
        print("空仓库：全量重放")
    elif head in remote_to_local:
        local_base = remote_to_local[head]
        print("远端 %s = %s (本地 %s)" % (BRANCH, head[:9], local_base[:9]))
    else:
        # 映射表丢失或远端被外部改动 —— 保守起见全量重放
        local_base = None
        print("远端 %s = %s 无映射记录，全量重放（会重建历史）" % (BRANCH, head[:9]))

    rng = "%s..HEAD" % local_base if local_base else "HEAD"
    shas = git("rev-list", "--reverse", rng).split()
    if not shas:
        print("没有新提交")
        return 0
    print("需要推送 %d 个提交" % len(shas))

    blob_cache = {}
    parent = head
    for n, sha in enumerate(shas, 1):
        message = git("log", "-1", "--format=%B", sha).rstrip()
        entries = []
        for line in git("ls-tree", "-r", sha).splitlines():
            meta, path = line.split("\t", 1)
            mode, _type, bhash = meta.split()
            if bhash not in blob_cache:
                content = subprocess.run(["git", "cat-file", "blob", bhash],
                                         capture_output=True, check=True).stdout
                blob_cache[bhash] = api("POST", "/repos/%s/git/blobs" % REPO, {
                    "content": base64.b64encode(content).decode(),
                    "encoding": "base64"})["sha"]
            entries.append({"path": path, "mode": mode, "type": "blob",
                            "sha": blob_cache[bhash]})
        tree = api("POST", "/repos/%s/git/trees" % REPO, {"tree": entries})["sha"]
        body = {"message": message, "tree": tree}
        if parent:
            body["parents"] = [parent]
        parent = api("POST", "/repos/%s/git/commits" % REPO, body)["sha"]
        mapping[sha] = parent
        print("  [%d/%d] %s %s (%d 文件)" % (n, len(shas), parent[:9],
                                             message.splitlines()[0][:54],
                                             len(entries)))

    if head is None:
        api("POST", "/repos/%s/git/refs" % REPO,
            {"ref": "refs/heads/" + BRANCH, "sha": parent})
    else:
        api("PATCH", "/repos/%s/git/refs/heads/%s" % (REPO, BRANCH),
            {"sha": parent, "force": True})
    save_map(mapping)
    print("\n%s -> %s" % (BRANCH, parent[:9]))
    print("映射表已更新: %s (%d 条)" % (MAP_FILE, len(mapping)))
    print("https://github.com/" + REPO)
    return 0


if __name__ == "__main__":
    sys.exit(main())
