"""Post-commit tracking for git hooks.

Invocations:
  worktracker_hook.py install      install the hook in the current repository
  worktracker_hook.py post-commit  run by `.git/hooks/post-commit`
  worktracker_hook.py flush        retry queued commits
  worktracker_hook.py status       show configuration and queue size

`install` is interactive when the URL or token is missing: it pre-fills saved
values, validates the credential before writing anything, and asks for
confirmation. Scripts can skip the prompts:

  install --url https://ai.example.com --token wt_hook_... --no-prompt
  install --interactive    # always review the wizard, even with saved values

The hook reads `$GIT_DIR/worktracker.env` (mode 0600) for the tracker URL and
token. Anything it cannot deliver is appended to `$GIT_DIR/worktracker-queue.jsonl`
and retried on the next commit, so a router outage never blocks a commit.

Only stdlib is used, so the installer can drop this single file anywhere.
"""
from __future__ import annotations

import getpass
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
from typing import TextIO, cast

TIMEOUT = 8
__version__ = "1.1.0"
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


def default_device() -> str:
    return os.uname().nodename if hasattr(os, "uname") else "unknown"


def tracker_config(cwd: str | None = None) -> dict[str, str]:
    values = parse_env(git_dir(cwd) / "worktracker.env")
    values.update({k: v for k, v in os.environ.items() if k.startswith("WORKTRACKER_")})
    url = os.environ.get("WORKTRACKER_URL") or values.get("WORKTRACKER_URL", "")
    token = os.environ.get("WORKTRACKER_TOKEN") or values.get("WORKTRACKER_TOKEN", "")
    device = values.get("WORKTRACKER_DEVICE") or default_device()
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
    except (urllib.error.URLError, OSError, ValueError):
        # tracker unreachable (or a malformed URL): callers queue and retry
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


_LAST_CHECK: tuple[int, dict] | None = None


class _Console:
    """Prompt on the controlling terminal, so `curl | sh` keeps working."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream

    def __enter__(self) -> _Console:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stream.close()

    def say(self, text: str = "") -> None:
        self.stream.write(text + "\n")
        self.stream.flush()

    def ask(self, label: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        self.stream.write(f"{label}{suffix}: ")
        self.stream.flush()
        line = self.stream.readline()
        if line == "":
            raise SystemExit("cancelled")
        return line.strip() or default

    def secret(self, label: str, default: str = "") -> str:
        suffix = " [keep the saved token]" if default else ""
        try:
            value = _read_secret(f"{label}{suffix}: ", self.stream)
        except EOFError:
            raise SystemExit("cancelled") from None
        return value.strip() or default

    def confirm(self, label: str, default: bool = True) -> bool:
        hint = "[Y/n]" if default else "[y/N]"
        self.stream.write(f"{label} {hint} ")
        self.stream.flush()
        line = self.stream.readline()
        if line == "":
            raise SystemExit("cancelled")
        answer = line.strip().lower()
        if not answer:
            return default
        return answer in ("y", "yes")


def _read_secret(label: str, stream: TextIO) -> str:
    """Separate hook for tests: getpass reads the real terminal, not stdin."""
    return getpass.getpass(label, stream=stream)


class _NoTerminal(Exception):
    """Raised when /dev/tty exists but this process has no controlling terminal."""


class _StdioStream:
    """Read stdin and write stdout when the process has no controlling TTY."""

    def readline(self) -> str:
        return sys.stdin.readline()

    def write(self, text: str) -> int:
        return sys.stdout.write(text)

    def flush(self) -> None:
        sys.stdout.flush()

    def close(self) -> None:
        pass

    def fileno(self) -> int:
        # getpass uses this to disable echo on the real terminal.
        return sys.stdin.fileno()


def _open_console() -> _Console:
    stream: TextIO
    try:
        stream = open("/dev/tty", "r+")  # noqa: SIM115 - closed by _Console
    except OSError:
        # Some sandboxes and IDE task runners give the process a terminal on
        # stdin but no controlling TTY, so /dev/tty is unopenable.
        try:
            interactive_stdin = sys.stdin.isatty()
        except (OSError, ValueError):
            interactive_stdin = False
        if not interactive_stdin:
            raise _NoTerminal() from None
        stream = cast(TextIO, _StdioStream())
    return _Console(stream)


def _normalize_url(raw: str) -> str:
    """Accept a bare host, strip a pasted API path, and reject nonsense."""
    value = raw.strip()
    if not value:
        raise ValueError("a tracker URL is required")
    if "://" not in value:
        value = "https://" + value
    if any(char.isspace() for char in value):
        raise ValueError("the URL must not contain spaces")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("start with http:// or https://")
    if not parsed.netloc:
        raise ValueError("include a host, e.g. https://ai.example.com")
    if parsed.query or parsed.fragment:
        raise ValueError("drop the ?query or #fragment part")
    path = parsed.path.rstrip("/")
    for suffix in ("/worktracker/api", "/worktracker"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _mask_secret(token: str) -> str:
    if not token:
        return "(none - open tracker)"
    if len(token) <= 10:
        return "*" * len(token)
    return f"{token[:4]}...{token[-4:]}"


def _hook_note(target: pathlib.Path) -> str:
    if not target.exists():
        return f"create {target}"
    if DEFAULT_MARKER in target.read_text():
        return f"update the Worktracker block in {target}"
    return f"append to the existing hook in {target}"


def _wizard(gd: pathlib.Path, cwd: str | None, url: str, token: str,
            device: str | None) -> tuple[str, str, str, tuple[int, dict]]:
    """Guided setup: defaults, validation with retry, then one confirmation."""
    saved = parse_env(gd / "worktracker.env")
    url = url or saved.get("WORKTRACKER_URL", "")
    token = token or saved.get("WORKTRACKER_TOKEN", "")
    if device is None:
        device = saved.get("WORKTRACKER_DEVICE", "") or default_device()
    try:
        root = git("rev-parse", "--show-toplevel", cwd=cwd).strip()
    except RuntimeError:
        root = cwd or os.getcwd()
    try:
        remote = git("config", "--get", "remote.origin.url", cwd=cwd).strip()
    except RuntimeError:
        remote = ""
    target = gd / "hooks" / "post-commit"
    note = _hook_note(target)

    with _open_console() as console:
        console.say("Worktracker git hook setup")
        console.say(f"  repository: {root}")
        console.say(f"  project:    {_project_key(remote, root)}")
        console.say(f"  hook:       {note}")
        console.say()

        while True:
            try:
                url = _normalize_url(
                    console.ask("Tracker URL (e.g. https://ai.example.com)", url))
                break
            except ValueError as exc:
                console.say(f"  ! {exc}")

        token = console.secret("Hook token (scope hook)", token)
        if not token:
            console.say("  no token: this only works where tracker auth is disabled")

        status: int = 0
        body: dict[str, object] = {}
        rejected = 0
        while True:
            console.say("Checking the credential...")
            status, body = request(url, token, "whoami", method="GET")
            if 200 <= status < 300:
                who = str(body.get("scope", "?"))
                if body.get("name"):
                    who += f", {body['name']}"
                console.say(f"  authenticated as {who}")
                break
            if status in (401, 403):
                rejected += 1
                if rejected >= 3:
                    raise SystemExit(
                        f"credential rejected by {url} (HTTP {status}); nothing installed")
                console.say(f"  ! token rejected (HTTP {status})")
                token = console.secret("Hook token (scope hook)")
                while not token:
                    console.say("  a hook token is required: tracker UI -> API Keys")
                    token = console.secret("Hook token (scope hook)")
                continue
            if status == 0:
                console.say(f"  ! {url} is unreachable from here")
                if console.confirm(
                        "Install anyway? Commits will be queued until it is reachable", True):
                    break
                raise SystemExit("aborted: tracker unreachable; nothing installed")
            console.say(f"  ! the credential check returned HTTP {status}")
            if console.confirm("Install anyway?", False):
                break
            raise SystemExit(
                f"aborted: credential check failed (HTTP {status}); nothing installed")

        device = console.ask("Device name", device) or default_device()

        console.say()
        console.say("Install summary")
        console.say(f"  repository: {root}")
        console.say(f"  tracker:    {url}")
        token_line = _mask_secret(token)
        if body.get("scope"):
            token_line += f" (scope {body['scope']})"
        console.say(f"  token:      {token_line}")
        console.say(f"  device:     {device}")
        console.say(f"  hook:       {note}")
        if not console.confirm("Install these?", True):
            raise SystemExit("aborted: nothing installed")
    return url, token, device, (status, body)


def _write_install(gd: pathlib.Path, url: str, token: str,
                   device: str | None) -> pathlib.Path:
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


def install(cwd: str | None = None, url: str = "", token: str = "", prompt: bool = True,
            device: str | None = None, interactive: bool = False) -> pathlib.Path:
    global _LAST_CHECK
    _LAST_CHECK = None
    gd = git_dir(cwd)
    url = url or os.environ.get("WORKTRACKER_URL", "")
    token = token or os.environ.get("WORKTRACKER_TOKEN", "")
    if prompt and (interactive or not url or not token):
        try:
            url, token, device, _LAST_CHECK = _wizard(gd, cwd, url, token, device)
        except _NoTerminal:
            # Re-running in a container/CI with saved credentials must keep
            # working; a forced --interactive still needs a real terminal.
            saved = parse_env(gd / "worktracker.env")
            saved_url = saved.get("WORKTRACKER_URL", "")
            saved_token = saved.get("WORKTRACKER_TOKEN", "")
            if interactive or not saved_url or not saved_token:
                raise SystemExit(
                    "no terminal available: this shell has no controlling TTY\n"
                    "(`test -t 0` fails here). Run the installer from an\n"
                    "interactive terminal, or pass --url and --token (or set\n"
                    "WORKTRACKER_URL/WORKTRACKER_TOKEN).") from None
            url, token = saved_url, saved_token
            device = device or saved.get("WORKTRACKER_DEVICE", "")
    elif not url or not token:
        raise SystemExit("missing credentials: pass --url and --token or set "
                         "WORKTRACKER_URL/WORKTRACKER_TOKEN")
    return _write_install(gd, url, token, device)


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "post-commit"
    args = dict(zip(argv[2::2], argv[3::2], strict=False))
    if command in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if command == "version":
        print(f"worktracker hook {__version__}")
        return 0
    if command == "post-commit":
        return post_commit()
    if command == "flush":
        print(f"delivered {flush()} queued commits")
        return 0
    if command == "install":
        if "--help" in argv or "-h" in argv:
            print(__doc__)
            return 0
        prompt = "--no-prompt" not in argv
        if not prompt:
            url = args.get("--url", "") or os.environ.get("WORKTRACKER_URL", "")
            token = args.get("--token", "") or os.environ.get("WORKTRACKER_TOKEN", "")
            if not url or not token:
                print("missing credentials: pass --url and --token or set "
                      "WORKTRACKER_URL/WORKTRACKER_TOKEN", file=sys.stderr)
                return 2
        target = install(url=args.get("--url", ""), token=args.get("--token", ""),
                         prompt=prompt, interactive="--interactive" in argv)
        if _LAST_CHECK is not None:
            status, body = _LAST_CHECK
        else:
            config = tracker_config()
            status, body = request(config["url"], config["token"], "whoami", method="GET")
        if 200 <= status < 300:
            print(f"hook installed at {target} (authenticated as {body.get('scope', '?')})")
        elif status == 0:
            print(f"hook installed at {target}, but the tracker was unreachable; "
                  "commits will be queued")
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
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        sys.exit(130)
