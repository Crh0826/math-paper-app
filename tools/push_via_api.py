# -*- coding: utf-8 -*-
"""带重试的 API 推送（github.com:443 在 git 协议上被挡，api.github.com 可用）。"""
import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = r"E:\OCR\repo"
OWNER, NAME = "Crh0826", "math-paper-app"
BRANCH = "master"
GIT = r"C:\Program Files\Git\cmd\git.exe"
API = "https://api.github.com"
DRY = "--dry-run" in sys.argv


def sh(args, cwd=REPO, check=True):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"{args} 失败：{r.stderr[:300]}")
    return r.stdout


TOKEN = sh(["gh", "auth", "token"], cwd=os.path.expanduser("~")).strip()


def api(method, path, payload=None, tries=4):
    url = API + path
    data = None
    headers = {"Authorization": "Bearer " + TOKEN, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dsh-push"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    last = None
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code in (401, 403, 404, 422):
                raise RuntimeError(f"{method} {path} → {exc.code}: {detail}")
            last = f"HTTP {exc.code}: {detail}"
        except (socket.timeout, TimeoutError, urllib.error.URLError, ConnectionError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt < tries:
            wait = 2 * attempt
            print(f"   重试 {attempt}/{tries-1}（{last[:60]}）等 {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"{method} {path} 连续失败：{last}")


remote_sha = api("GET", f"/repos/{OWNER}/{NAME}/git/ref/heads/{BRANCH}")["object"]["sha"]
local_sha = sh([GIT, "rev-parse", "HEAD"]).strip()
print("远端 HEAD:", remote_sha[:8], " 本地 HEAD:", local_sha[:8])

if remote_sha == local_sha:
    print("已经一致")
    raise SystemExit(0)

out = sh([GIT, "diff", "--name-status", remote_sha, local_sha], check=False)
changes = []
for line in out.splitlines():
    parts = line.split("\t")
    if len(parts) == 2:
        changes.append((parts[0], parts[1]))
    elif len(parts) == 3:
        changes.append((parts[0][0], parts[2]))

adds = [p for st, p in changes if st in ("A", "M", "R", "C")]
dels = [p for st, p in changes if st == "D"]
print(f"新增/修改 {len(adds)}，删除 {len(dels)}")
if DRY:
    for p in adds:
        print("   +", p)
    raise SystemExit(0)

tree_items = []
for i, rel in enumerate(adds, 1):
    path = os.path.join(REPO, rel)
    if not os.path.isfile(path):
        print("  跳过（本地没有该路径）", rel)
        continue
    with open(path, "rb") as fh:
        content = base64.b64encode(fh.read()).decode("ascii")
    blob = api("POST", f"/repos/{OWNER}/{NAME}/git/blobs",
               {"content": content, "encoding": "base64"})
    tree_items.append({"path": rel.replace(os.sep, "/"), "mode": "100644",
                       "type": "blob", "sha": blob["sha"]})
    if i % 20 == 0 or i == len(adds):
        print(f"  blob {i}/{len(adds)}")

for rel in dels:
    tree_items.append({"path": rel.replace(os.sep, "/"), "mode": "100644",
                       "type": "blob", "sha": None})

base_tree = api("GET", f"/repos/{OWNER}/{NAME}/git/commits/{remote_sha}")["tree"]["sha"]
tree = api("POST", f"/repos/{OWNER}/{NAME}/git/trees",
           {"base_tree": base_tree, "tree": tree_items})
print("新 tree:", tree["sha"][:8], f"（{len(tree_items)} 项）")

message = "feat: 物理卷带图导入 + 组卷页 UI 收敛（2026-11-07）\n\n" + \
          sh([GIT, "log", "-1", "--pretty=%b", "HEAD"]).strip()[:2500]
commit = api("POST", f"/repos/{OWNER}/{NAME}/git/commits",
             {"message": message, "tree": tree["sha"], "parents": [remote_sha]})
print("新 commit:", commit["sha"][:8])

api("PATCH", f"/repos/{OWNER}/{NAME}/git/refs/heads/{BRANCH}",
    {"sha": commit["sha"], "force": False})
print("已推送到", f"https://github.com/{OWNER}/{NAME}")
