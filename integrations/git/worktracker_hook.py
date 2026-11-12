"""Post-commit tracking for git hooks.

Invocations:
  worktracker_hook.py install      install the hook in the current repository
  worktracker_hook.py post-commit  run by `.git/hooks/post-commit`
  worktracker_hook.py flush        retry queued commits
  worktracker_hook.py status       show configuration and queue size

The hook reads `$GIT_DIR/worktracker.env` (mode 0600) for the tracker URL and
token. Anything it cannot deliver is appended to `$GIT_DIR/worktracker-queue.jsonl`
and retried on the next commit, so a router outage never blocks a commit.

Only stdlib is used, so the installer can drop this single file anywhere.
"""
from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 8
__version__ = "1.0.0"
DEFAULT_MARKER = "# >>> worktracker hook >>>"


def git(*args: str, cwd: str | None = None) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "git failed")
    return out.stdout


def git_dir(cwd: str | None = None) -> pathlib.Path:
    try:
        return pathlib.Path(git("rev-parse", "--absolute-git-dir", cwd=cwd).strip())
    except RuntimeError:
        # Not inside a repository (or the directory is gone): callers treat an
        # empty/missing config as "hook disabled" instead of crashing a commit.
        base = pathlib.Path(cwd) if cwd else pathlib.Path.cwd()
        return base / ".git"


def parse_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def tracker_config(cwd: str | None = None) -> dict[str, str]:
    values = parse_env(git_dir(cwd) / "worktracker.env")
    values.update({k: v for k, v in os.environ.items() if k.startswith("WORKTRACKER_")})
    url = os.environ.get("WORKTRACKER_URL") or values.get("WORKTRACKER_URL", "")
    token = os.environ.get("WORKTRACKER_TOKEN") or values.get("WORKTRACKER_TOKEN", "")
    device = values.get("WORKTRACKER_DEVICE") or os.uname().nodename if hasattr(os, "uname") else "unknown"
    return {"url": url.rstrip("/"), "token": token, "device": device}


def api_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/worktracker/api/" + path.lstrip("/")


def request(base: str, token: str, path: str, payload: dict | None = None,
            method: str = "POST") -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(api_url(base, path), data=data, method=method)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except Exception:
            body = {}
        return exc.code, body
    except (urllib.error.URLError, OSError):
        # tracker unreachable: caller queues and retries on the next commit
        return 0, {}


def commit_payload(sha: str, cwd: str | None = None) -> dict:
    fmt = "%H%x00%P%x00%an%x00%ae%x00%aI%x00%s%x00%b"
    raw = git("show", "-s", f"--format={fmt}", sha, cwd=cwd)
    full, parents, author, email, committed, subject, body = (raw.split("\x00") + [""] * 7)[:7]
    numstat = git("show", "--numstat", "--format=", sha, cwd=cwd)
    files = []
    for line in numstat.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add, delete, path = parts[0], parts[1], parts[2]
        files.append({
            "path": path,
            "additions": int(add) if add.isdigit() else 0,
            "deletions": int(delete) if delete.isdigit() else 0,
            "binary": not add.isdigit(),
        })
    try:
        branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd).strip()
    except RuntimeError:
        branch = ""
    remote = ""
    try:
        remote = git("config", "--get", "remote.origin.url", cwd=cwd).strip()
    except RuntimeError:
        pass
    try:
        root = git("rev-parse", "--show-toplevel", cwd=cwd).strip()
    except RuntimeError:
        root = cwd or os.getcwd()
    return {
        "sha": full or sha,
        "parents": [p for p in parents.split() if p],
        "author_name": author,
        "author_email": email,
        "committed_at": committed or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "subject": subject,
        "body": body.strip(),
        "files": files,
        "additions": sum(f["additions"] for f in files),
        "deletions": sum(f["deletions"] for f in files),
        "branch": branch if branch and branch != "HEAD" else None,
        "repo_remote": remote or None,
        "repo_root": root,
        "project": _project_key(remote, root),
        "source": "hook",
    }


def _project_key(remote: str, root: str) -> str:
    if remote:
        raw = remote.strip()
        if "://" in raw:
            parsed = urllib.parse.urlsplit(raw)
            host = (parsed.hostname or "").lower()
            path = parsed.path
        else:
            host_path = raw.split("@", 1)[-1]
            if ":" in host_path:
                host, _, path = host_path.partition(":")
            else:
                host = host_path
                path = ""
            host = host.lower()
        path = "/" + path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        if host:
            return f"{host}{path}"
        return path.strip("/")
    return f"path:{root}"


def queue_file(cwd: str | None = None) -> pathlib.Path:
    return git_dir(cwd) / "worktracker-queue.jsonl"


def enqueue(payload: dict, cwd: str | None = None) -> None:
    path = queue_file(cwd)
    with path.open("a") as handle:
        handle.write(json.dumps({"queued_at": time.time(), "payload": payload}) + "\n")


def flush(cwd: str | None = None, limit: int = 50) -> int:
    """Deliver queued commits; returns how many were sent."""
    config = tracker_config(cwd)
    path = queue_file(cwd)
    if not config["url"] or not path.exists():
        return 0
    remaining: list[str] = []
    sent = 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
                raise ValueError("malformed queue entry")
        except (json.JSONDecodeError, ValueError):
            continue  # drop corrupted entries so they cannot block the queue
        if sent >= limit:
            remaining.append(line)
            continue
        status, _ = request(config["url"], config["token"], "commits", item.get("payload") or {})
        if 200 <= status < 300:
            sent += 1
        else:
            remaining.append(line)
    if remaining:
        path.write_text("\n".join(remaining) + "\n")
    elif path.exists():
        path.unlink()
    return sent


def flush_pending(cwd: str | None = None, config: dict | None = None) -> bool:
    """Deliver the backlog unless it is already not being accepted.

    When queued commits cannot be delivered the tracker is likely down, so the
    new commit is queued immediately instead of waiting for another timeout
    (git's post-commit hook must stay fast). Returns True when the tracker is
    healthy.
    """
    path = queue_file(cwd)
    if not path.exists():
        return True
    sent = flush(cwd)
    return sent > 0 or not path.exists()


def post_commit(cwd: str | None = None) -> int:
    config = tracker_config(cwd)
    try:
        sha = git("rev-parse", "HEAD", cwd=cwd).strip()
    except RuntimeError:
        return 0  # e.g. the initial commit of an empty branch history
    if not config["url"]:
        return 0
    if flush_pending(cwd, config) is False:
        # Keep the new commit in git's own history: it goes to the queue too,
        # so nothing is lost when a burst of commits hits an outage.
        enqueue(commit_payload(sha, cwd), cwd)
        return 0
    payload = commit_payload(sha, cwd)
    status, _ = request(config["url"], config["token"], "commits", payload)
    if 200 <= status < 300:
        return 0
    enqueue(payload, cwd)
    return 0


HOOK_SCRIPT = f"""{DEFAULT_MARKER}
# Installed by `worktracker_hook.py install`. Remove this block to disable.
_worktracker_hook_file="${{WORKTRACKER_HOOK_FILE:-$0}}"
_python="$(command -v python3 || true)"
if [ -n "$_python" ] && [ -f "$_worktracker_hook_file.d/post_commit.py" ]; then
  "$_python" "$_worktracker_hook_file.d/post_commit.py" post-commit >/dev/null 2>&1 || true
fi
# <<< worktracker hook <<<
"""


def install(cwd: str | None = None, url: str = "", token: str = "", prompt: bool = True,
            device: str | None = None) -> pathlib.Path:
    gd = git_dir(cwd)
    url = url or os.environ.get("WORKTRACKER_URL", "")
    token = token or os.environ.get("WORKTRACKER_TOKEN", "")
    if prompt and (not url or not token):
        url, token = _prompt(url, token)
    if device is None:
        # Keep a device name already configured by an earlier install.
        device = parse_env(gd / "worktracker.env").get("WORKTRACKER_DEVICE", "")
    env_path = gd / "worktracker.env"
    env_path.write_text(
        "# worktracker hook credentials - do not commit\n"
        f"WORKTRACKER_URL={url.rstrip('/')}\n"
        f"WORKTRACKER_TOKEN={token}\n"
        + (f"WORKTRACKER_DEVICE={device}\n" if device else ""))
    env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    hooks = gd / "hooks"
    hooks.mkdir(exist_ok=True)
    target = hooks / "post-commit"
    ours = pathlib.Path(__file__).resolve()
    payload_dir = pathlib.Path(f"{target}.d")
    payload_dir.mkdir(exist_ok=True)
    (payload_dir / "post_commit.py").write_text(ours.read_text())
    existing = target.read_text() if target.exists() else "#!/bin/sh\n"
    if DEFAULT_MARKER not in existing:
        if not existing.endswith("\n"):
            existing += "\n"
        existing += HOOK_SCRIPT
    target.write_text(existing)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def _prompt(url: str, token: str) -> tuple[str, str]:
    """Ask on /dev/tty so the installer keeps working through `curl | sh`."""
    try:
        tty = open("/dev/tty", "r+")  # noqa: SIM115 - interactive prompt
    except OSError:
        raise SystemExit("no terminal available: pass --url and --token") from None
    with tty:
        if not url:
            tty.write("Tracker URL (e.g. https://ai.example.com): ")
            tty.flush()
            url = tty.readline().strip()
        if not token:
            import getpass

            token = getpass.getpass("Tracker hook token: ", stream=tty).strip()
    return url.rstrip("/"), token


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "post-commit"
    args = dict(zip(argv[2::2], argv[3::2], strict=False))
    if command == "version":
        print(f"worktracker hook {__version__}")
        return 0
    if command == "post-commit":
        return post_commit()
    if command == "flush":
        print(f"delivered {flush()} queued commits")
        return 0
    if command == "install":
        target = install(url=args.get("--url", ""), token=args.get("--token", ""),
                         prompt="--no-prompt" not in argv)
        config = tracker_config()
        status, body = request(config["url"], config["token"], "whoami", method="GET")
        if 200 <= status < 300:
            print(f"hook installed at {target} (authenticated as {body.get('scope', '?')})")
        else:
            print(f"hook installed at {target}, but the credential check failed: HTTP {status}")
        return 0
    if command == "status":
        config = tracker_config()
        print(json.dumps({"url": config["url"], "token": "set" if config["token"] else "missing",
                          "queue": len(queue_file().read_text().splitlines())
                          if queue_file().exists() else 0}, indent=2))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
