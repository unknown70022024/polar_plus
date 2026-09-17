#!/usr/bin/env python3
"""把 ~/.lightning.env 里的密钥写入 GitHub Actions Secrets。

用法：
    python tools/set_secrets.py [owner/repo]

需要 ~/.gh-token（一个含 repo + workflow scope 的 PAT）。
密钥值从不打印，只报长度。
"""
import base64
import json
import pathlib
import sys
import urllib.error
import urllib.request

from nacl.encoding import Base64Encoder
from nacl.public import PublicKey, SealedBox

API = "https://api.github.com"
REPO = sys.argv[1] if len(sys.argv) > 1 else "unknown70022024/lightning-pipeline"
ENV_FILE = pathlib.Path.home() / ".lightning.env"
WANTED = ("EUMETSAT_CONSUMER_KEY", "EUMETSAT_CONSUMER_SECRET")


def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_FILE.is_file():
        print(f"找不到 {ENV_FILE}")
        return out
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        if k.strip() in WANTED and v:
            out[k.strip()] = v
    return out


def main() -> int:
    token = pathlib.Path.home().joinpath(".gh-token").read_text().strip()
    values = load_env()
    missing = [k for k in WANTED if k not in values]
    if missing:
        print("~/.lightning.env 里缺：", ", ".join(missing))
        print(f"请编辑 {ENV_FILE} 填入后重跑")
        return 1

    def api(method, path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(
            API + path, method=method, data=body,
            headers={"Authorization": f"token {token}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}

    key = api("GET", f"/repos/{REPO}/actions/secrets/public-key")
    pub = PublicKey(key["key"].encode(), Base64Encoder())
    box = SealedBox(pub)

    for name, value in values.items():
        sealed = box.encrypt(value.encode())
        api("PUT", f"/repos/{REPO}/actions/secrets/{name}", {
            "encrypted_value": base64.b64encode(sealed).decode(),
            "key_id": key["key_id"],
        })
        print(f"  已写入 {name}（长度 {len(value)}，值未打印）")

    listed = api("GET", f"/repos/{REPO}/actions/secrets")["secrets"]
    print("\n远端现有 secrets：", ", ".join(s["name"] for s in listed) or "(无)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
