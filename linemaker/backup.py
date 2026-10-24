"""Nightly backup of everything the bot has learned, to a private GitHub repo.

Everything that can't be rebuilt lives only on the server: tracking/ (every priced market and
its result, which the weights are fit on), weights.json, bets.json and the frozen pick
snapshots. If the server is lost, all of it goes with it. This copies them (JSON gzipped,
only files that changed) into a private repo and pushes:

  LINEMAKER_BACKUP_REPO   owner/name of an empty PRIVATE repo, e.g. ProdWavvy/linemaker-data
  LINEMAKER_BACKUP_TOKEN  a GitHub fine-grained token with Contents: Read and write on that
                          repo only (set it with /set; it's never written to disk or logs)

Runs after the morning grade and with /backup. To restore on a new server: clone the repo,
gunzip each file back into ~/linemaker/ under the same path (tracking/..., snapshots/...).
"""
import base64
import gzip
import logging
import os
import shutil
import subprocess
import time

log = logging.getLogger("linemaker.backup")
README = """# Linemaker data backup

Written by the bot after every morning grade. Restore on a new server:

    cd ~/linemaker
    for f in $(cd ~/linemaker-data && find tracking snapshots -name '*.gz'); do
      mkdir -p "$(dirname "${f%.gz}")"; gunzip -c ~/linemaker-data/"$f" > "${f%.gz}"
    done
"""


def configured() -> bool:
    return bool(os.environ.get("LINEMAKER_BACKUP_REPO") and os.environ.get("LINEMAKER_BACKUP_TOKEN"))


def _git(args: list, cwd: str, token: str) -> str:
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    cmd = ["git", "-c", f"http.extraHeader=AUTHORIZATION: basic {auth}", "-c", "user.name=Linemaker",
           "-c", "user.email=linemaker@localhost"] + args
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)
    out = (p.stdout + p.stderr).replace(token, "<token>").replace(auth, "<auth>")
    if p.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {out.strip()[-300:]}")
    return out


def _sources(root: str) -> list:
    """(source path, path in the backup) for every file worth keeping."""
    tdir = os.environ.get("LINEMAKER_TRACKING_DIR", "tracking")
    sdir = os.environ.get("LINEMAKER_SNAPSHOTS", "snapshots")
    out = []
    for base in (tdir, sdir):
        full = base if os.path.isabs(base) else os.path.join(root, base)
        for dirpath, _dirs, files in os.walk(full):
            for name in files:
                if name.endswith(".json"):
                    src = os.path.join(dirpath, name)
                    rel = os.path.join(os.path.basename(base.rstrip("/")), os.path.relpath(src, full))
                    out.append((src, rel + ".gz"))
    return out


def run_backup(root: str = None) -> str:
    """Copy changed files into the backup clone, commit and push. Returns a one-line summary."""
    from .envfile import root_dir
    root = root or root_dir()
    repo, token = os.environ.get("LINEMAKER_BACKUP_REPO", "").strip(), os.environ.get("LINEMAKER_BACKUP_TOKEN", "").strip()
    if not repo or not token:
        return "Backup isn't set up: /set LINEMAKER_BACKUP_REPO and LINEMAKER_BACKUP_TOKEN first."
    work = os.path.join(root, os.environ.get("LINEMAKER_CACHE_DIR", "cache"), "backup_repo")
    url = os.environ.get("LINEMAKER_BACKUP_URL") or f"https://github.com/{repo}.git"   # URL override: tests
    if not os.path.isdir(os.path.join(work, ".git")):
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(os.path.dirname(work), exist_ok=True)
        _git(["clone", "-q", url, work], root, token)
    else:
        try:
            _git(["pull", "-q", "--rebase", "origin", "HEAD"], work, token)
        except RuntimeError:
            pass   # empty repo (no commits yet): nothing to pull
    import json
    mpath = os.path.join(work, "manifest.json")   # path -> source mtime when last backed up
    try:
        with open(mpath) as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        manifest = {}
    changed = 0
    for src, rel in _sources(root):
        dst, mtime = os.path.join(work, rel), os.path.getmtime(src)
        if os.path.exists(dst) and manifest.get(rel) == mtime:
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, "rb") as f, gzip.GzipFile(dst, "wb", compresslevel=6, mtime=0) as g:
            g.write(f.read())
        manifest[rel] = mtime
        changed += 1
    with open(mpath, "w") as f:
        json.dump(manifest, f, indent=0, sort_keys=True)
    with open(os.path.join(work, "README.md"), "w") as f:
        f.write(README)
    _git(["add", "-A"], work, token)
    status = _git(["status", "--porcelain"], work, token)
    if not status.strip():
        return "Backup: nothing changed since the last one."
    stamp = time.strftime("%Y-%m-%d %H:%M")
    _git(["commit", "-q", "-m", f"backup {stamp} ({changed} files)"], work, token)
    _git(["push", "-q", "origin", "HEAD:main"], work, token)
    log.info("backup pushed: %d files", changed)
    return f"💾 Backup pushed to {repo}: {changed} file(s) updated."
