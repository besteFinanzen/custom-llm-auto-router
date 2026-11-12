#!/usr/bin/env python3
"""End-to-end Worktracker verification against a running deployment.

Exercises the real surfaces (no mocks): usage ingest with the OpenCode
attribution payload, presence, git snapshots, tool events, WakaTime/VS Code
heartbeats, a real commit through the stdlib post-commit hook, commit
attribution, budgets (local + split-mode remote client), the read APIs and the
UI. Run it against a freshly deployed container + PostgreSQL database:

    TRACKER_ADMIN_TOKEN=… TRACKER_EDITOR_KEY=… \
    TRACKER_HOOK_TOKEN=… TRACKER_READ_TOKEN=… \
    WORKTRACKER_URL=http://localhost:4143 python3 scripts/e2e_worktracker.py

Only the Python standard library is used; the split-mode check additionally
imports ``worktracker.budgets`` from this checkout.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = os.environ.get("WORKTRACKER_URL", "http://localhost:4143").rstrip("/")
REPO = pathlib.Path(os.environ.get("WORKTRACKER_E2E_REPO", "/tmp/opencode/worktracker-e2e"))
ADMIN = {"authorization": f"Bearer {os.environ.get('TRACKER_ADMIN_TOKEN', '')}"}
HOOK = {"authorization": f"Bearer {os.environ.get('TRACKER_HOOK_TOKEN', '')}"}
EDITOR = {"authorization": f"Bearer {os.environ.get('TRACKER_EDITOR_KEY', '')}"}
READ = {"authorization": f"Bearer {os.environ.get('TRACKER_READ_TOKEN', '')}"}
checks: list[tuple[str, bool]] = []


def call(method: str, path: str, body=None, headers=None, raw: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("content-type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req) as response:
        text = response.read().decode()
    return text if raw else (json.loads(text) if text else {})


def ok(name: str, condition, detail: str = "") -> None:
    checks.append((name, bool(condition)))
    print(("PASS " if condition else "FAIL ") + name + (f" :: {detail}" if detail else ""))


def main() -> int:
    if REPO.exists():
        print(f"reusing {REPO}; remove it first for a clean run")
    now = dt.datetime.now(dt.timezone.utc)
    project = f"path:{REPO}"
    # Usage rows are immutable: fresh ids per run keep the script re-runnable.
    run_id = uuid.uuid4().hex[:8]

    def usage(event_id: str, cost: float, minutes: int, **extra):
        row = {
            "id": event_id, "ts": (now - dt.timedelta(minutes=minutes)).isoformat(),
            "session": "sess-e2e", "project_id": project, "project_root": str(REPO),
            "branch": "main", "device": "e2e-laptop", "tool": "opencode", "kind": "primary",
            "provider": "openai", "model": "openai/gpt-5", "tokens_in": 1200,
            "tokens_out": 300, "cost": cost, "latency_ms": 1500, "ttft_ms": 250,
            "outcome": "success",
        }
        row.update(extra)
        return row

    # 1. usage ingest with the plugin's attribution payload -----------------
    result = call("POST", "/worktracker/api/usage", [
        usage(str(uuid.UUID(f"{run_id}-0000-0000-0000-000000000001")), 0.42, 6),
        usage(str(uuid.UUID(f"{run_id}-0000-0000-0000-000000000002")), 0.18, 2,
              kind="auxiliary", job="summarize", tokens_in=400, tokens_out=80),
        usage(str(uuid.UUID(f"{run_id}-0000-0000-0000-000000000003")), 0.07, 1,
              project_id="path:/tmp/opencode/other", project_root="/tmp/opencode/other"),
    ], HOOK)
    ok("usage accepted", result.get("accepted") == 3, json.dumps(result))

    # 2. presence + git snapshot + tool event -------------------------------
    presence = call("POST", "/worktracker/api/presence", {
        "session": "sess-e2e", "project": project, "project_root": str(REPO),
        "branch": "main", "device": "e2e-laptop", "tool": "opencode",
        "file": "main.py", "model": "openai/gpt-5", "status": "active",
    }, HOOK)
    ok("presence upsert", bool(presence.get("presence")))
    ok("git snapshot", bool(call("POST", "/worktracker/api/snapshots", {
        "session": "sess-e2e", "project": project, "project_root": str(REPO),
        "branch": "main", "head": "0" * 40, "dirty": True, "files": ["main.py"],
        "device": "e2e-laptop", "tool": "opencode",
    }, HOOK).get("id")))
    ok("tool event", bool(call("POST", "/worktracker/api/tool-events", {
        "session": "sess-e2e", "project": project, "project_root": str(REPO),
        "branch": "main", "device": "e2e-laptop", "tool": "opencode",
        "name": "edit", "file": "main.py",
    }, HOOK).get("id")))

    # 3. WakaTime-compatible VS Code heartbeats -----------------------------
    heartbeat = {
        "entity": str(REPO / "main.py"), "type": "file", "time": now.timestamp(),
        "project": REPO.name, "branch": "main", "language": "Python",
        "is_write": True, "plugin": "vscode/1.98.0 vscode-wakatime/26.0.0",
    }
    waka = call("POST", "/api/v1/users/current/heartbeats", heartbeat, EDITOR)
    ok("wakatime single heartbeat", waka.get("responses"), json.dumps(waka)[:200])
    bulk = call("POST", "/api/v1/users/current/heartbeats.bulk",
                [heartbeat, {**heartbeat, "entity": str(REPO / "app.py"),
                             "time": now.timestamp() + 30}], EDITOR)
    ok("wakatime bulk heartbeats", len(bulk.get("responses", [])) == 2,
       json.dumps(bulk)[:200])
    ok("wakatime summaries", "data" in call(
        "GET", f"/api/v1/users/current/summaries?start={now:%Y-%m-%d}&end={now:%Y-%m-%d}",
        headers=EDITOR))

    # 4. real commit through the stdlib git hook ----------------------------
    REPO.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(REPO)], check=True)
    subprocess.run(["git", "-C", str(REPO), "config", "user.email", "e2e@example.com"],
                   check=True)
    subprocess.run(["git", "-C", str(REPO), "config", "user.name", "E2E"], check=True)
    (REPO / "main.py").write_text("print('hello worktracker')\n" * 20)
    subprocess.run(["git", "-C", str(REPO), "add", "main.py"], check=True)
    install = subprocess.run(
        ["sh", str(ROOT / "integrations" / "git" / "install-hook.sh")], cwd=REPO,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(REPO),
             "WORKTRACKER_URL": BASE,
             "WORKTRACKER_TOKEN": os.environ.get("TRACKER_HOOK_TOKEN", "")},
        capture_output=True, text=True)
    print("hook installer:", install.stdout.strip()[-400:], install.stderr.strip()[-300:])
    ok("hook installed", (REPO / ".git/hooks/post-commit.d/post_commit.py").exists()
       and (REPO / ".git/worktracker.env").exists())
    commit = subprocess.run(["git", "-C", str(REPO), "commit", "-q", "--allow-empty",
                             "-m", "feat: e2e tracked commit"], capture_output=True, text=True)
    sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    ok("commit created", commit.returncode == 0, sha)
    flush = subprocess.run(
        ["python3", str(REPO / ".git/hooks/post-commit.d/post_commit.py"), "flush"],
        capture_output=True, text=True)
    print("hook flush:", flush.stdout.strip()[-200:], flush.stderr.strip()[-200:])
    key = urllib.parse.quote(project, safe="")
    commits = call("GET", f"/worktracker/api/commits?key={key}", headers=READ)
    ok("commit delivered by hook", bool(commits.get("commits")), json.dumps(commits)[:300])
    if commits.get("commits"):
        detail = call("GET", f"/worktracker/api/commit?key={key}&sha={sha}", headers=READ)
        usage_rows = detail.get("usage", [])
        cost = round(sum(float(row.get("cost_usd") or 0) for row in usage_rows), 2)
        ok("commit records AI cost", cost > 0, f"attributed ${cost}")
        ok("commit has attribution evidence",
           bool(usage_rows) and all(row.get("evidence") for row in usage_rows))
        ok("commit links session and model",
           "sess-e2e" in json.dumps(detail) and "openai/gpt-5" in json.dumps(detail))

    # 5. budgets ------------------------------------------------------------
    budget = call("POST", "/worktracker/api/budgets", {
        "project": project, "branch": "main", "metric": "cost", "limit": 0.50,
        "period": "daily", "hard_block": True, "name": "e2e daily cap",
    }, ADMIN)
    ok("budget created", bool(budget.get("budget", {}).get("id")), json.dumps(budget)[:200])
    check = call("GET", f"/worktracker/api/budget/check?project_key={key}"
                        "&branch=main&reserve_usd=1.00", headers=READ)
    ok("budget hard blocks over-limit request", bool(check.get("blocked_by")),
       json.dumps(check)[:300])
    sys.path.insert(0, str(ROOT))
    from worktracker.budgets import RemoteBudgetClient

    remote = RemoteBudgetClient(BASE, os.environ.get("TRACKER_HOOK_TOKEN", "")).check(
        project, "main", reserve_usd=1.00)
    ok("split-mode remote budget client blocks", bool(remote.get("blocked_by")),
       json.dumps(remote)[:300])

    # 6. read surfaces ------------------------------------------------------
    for name, path in [
        ("overview", "/worktracker/api/overview?days=30"),
        ("projects", "/worktracker/api/projects?days=30"),
        ("models", "/worktracker/api/models?days=30"),
        ("providers", "/worktracker/api/providers?days=30"),
        ("sessions", "/worktracker/api/sessions?days=30"),
        ("presence list", "/worktracker/api/presence"),
        ("heatmap", "/worktracker/api/heatmap?days=30"),
        ("calendar", "/worktracker/api/calendar?days=30"),
        ("budgets", "/worktracker/api/budgets"),
        ("jobs", "/worktracker/api/jobs"),
    ]:
        body = call("GET", path, headers=READ)
        ok(f"GET {name}", bool(body), json.dumps(body)[:180])
    html = call("GET", "/worktracker", headers=READ, raw=True)
    ok("UI served", "<html" in html.lower() and len(html) > 5000, f"{len(html)} bytes")

    failed = [name for name, passed in checks if not passed]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} E2E checks passed against {BASE}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
