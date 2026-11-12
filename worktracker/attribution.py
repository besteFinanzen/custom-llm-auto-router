"""Request attribution validation.

Every LLM request must carry enough metadata to attribute spend to a project,
session, branch, device and tool.  The OpenCode plugin sends compact headers on
every request; ``x_oc`` in the JSON body is accepted as a fallback for other
clients.  Missing fields are reported individually with a concrete fix so the
caller can repair its integration instead of guessing.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

FIELD_HEADERS = {
    "kind": "x-oc-kind",
    "session": "x-oc-session",
    "project": "x-oc-project",
    "project_root": "x-oc-project-root",
    "session_directory": "x-oc-session-directory",
    "device": "x-oc-device",
    "tool": "x-oc-tool",
    "branch": "x-oc-branch",
    "git_head": "x-oc-git-head",
    "job": "x-oc-job",
    "agent": "x-oc-agent",
    "repo_remote": "x-oc-repo-remote",
}

FIXES = {
    "kind": "Send `x-oc-kind: primary` (or compaction/title/generate).",
    "session": "Send `x-oc-session` with the OpenCode session id; the router-tags plugin sets it automatically.",
    "project": "Send `x-oc-project` with the stable project id; the plugin uses the session project.",
    "project_root": "Send `x-oc-project-root` with the canonical project directory.",
    "session_directory": (
        "Send `x-oc-session-directory`; update the router-tags plugin to >=0.2.0, which "
        "resolves `ctx.session.get({sessionID}).location.directory` per session."
    ),
    "device": "Send `x-oc-device` with the hostname; the plugin sends `os.hostname()`.",
    "tool": "Send `x-oc-tool` with the client name (opencode, vscode, cursor, cli).",
    "branch": "Send `x-oc-branch`, or `x-oc-no-git: 1` when the session is outside a git repository.",
    "git_head": "Send `x-oc-git-head` with the current commit SHA; the plugin reads `git rev-parse HEAD`.",
}

REQUIRED = ("kind", "session", "project", "project_root", "session_directory",
            "device", "tool")

_ALIASES = {
    "session": ("session", "session_id", "sessionID"),
    "project": ("project", "project_id", "projectID"),
    "project_root": ("project_root", "projectRoot"),
    "session_directory": ("session_directory", "sessionDirectory", "directory"),
    "git_head": ("git_head", "gitHead", "head"),
    "repo_remote": ("repo_remote", "repoRemote", "remote"),
    "tool_version": ("tool_version", "toolVersion", "version"),
    "no_git": ("no_git", "noGit"),
}


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _body_value(body: Any, key: str) -> Any:
    if not isinstance(body, Mapping):
        return None
    candidates: list[Mapping] = []
    for container in (body.get("x_oc"), body.get("metadata")):
        if isinstance(container, Mapping):
            candidates.append(container)
            if isinstance(container.get("x_oc"), Mapping):
                candidates.append(container["x_oc"])
    for container in candidates:
        for name in _ALIASES.get(key, (key,)):
            value = container.get(name)
            if value not in (None, ""):
                return value
    return None


def extract(headers: Mapping[str, str], body: Any = None) -> dict[str, Any]:
    """Merge header and body attribution into one record."""
    lowered = {str(k).lower(): v for k, v in headers.items()}
    out: dict[str, Any] = {}
    for key, header in FIELD_HEADERS.items():
        value = lowered.get(header)
        if value in (None, ""):
            value = _body_value(body, key)
        if value not in (None, ""):
            out[key] = str(value).strip()
    no_git = lowered.get("x-oc-no-git")
    if no_git in (None, ""):
        no_git = _body_value(body, "no_git")
    out["no_git"] = _truthy(no_git) if no_git is not None else False
    for key in ("tool_version", "client"):
        value = lowered.get(f"x-oc-{key.replace('_', '-')}") or _body_value(body, key)
        if value not in (None, ""):
            out[key] = str(value).strip()
    return out


def missing(meta: Mapping[str, Any]) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    for field in REQUIRED:
        if not meta.get(field):
            problems.append({
                "field": field,
                "header": FIELD_HEADERS[field],
                "problem": f"missing {FIELD_HEADERS[field]}",
                "fix": FIXES[field],
            })
    if not meta.get("branch"):
        if meta.get("no_git"):
            pass
        else:
            problems.append({
                "field": "branch",
                "header": FIELD_HEADERS["branch"],
                "problem": "missing x-oc-branch (and x-oc-no-git is not set)",
                "fix": FIXES["branch"],
            })
        # No git-head problem when the plugin explicitly reports a non-git session.
        if meta.get("no_git"):
            return problems
    if meta.get("branch") and not meta.get("git_head"):
        problems.append({
            "field": "git_head",
            "header": FIELD_HEADERS["git_head"],
            "problem": "missing x-oc-git-head for a git session",
            "fix": FIXES["git_head"],
        })
    return problems


def apply(record: dict[str, Any], meta: Mapping[str, Any]) -> dict[str, Any]:
    """Copy attribution fields onto a usage record (no empty overwrites)."""
    mapping = {
        "session": "session",
        "project": "project_id",
        "project_root": "project_root",
        "session_directory": "session_directory",
        "branch": "branch",
        "device": "device",
        "tool": "tool",
        "tool_version": "tool_version",
        "job": "job",
        "agent": "agent",
        "git_head": "git_head",
        "repo_remote": "repo_remote",
    }
    for source, target in mapping.items():
        if meta.get(source):
            record[target] = meta[source]
    if meta.get("kind"):
        record["kind"] = meta["kind"]
    record.setdefault("metadata", {})
    if isinstance(record["metadata"], dict):
        record["metadata"]["no_git"] = bool(meta.get("no_git"))
    return record


def error_payload(problems: list[dict[str, str]], *, path: str | None = None) -> dict:
    names = ", ".join(p["header"] for p in problems)
    return {
        "error": {
            "message": f"Missing AI tracking metadata: {names}. "
                       "Fix the listed headers and retry; no request was sent upstream.",
            "type": "attribution_error",
            "code": "missing_attribution",
            "param": problems[0]["header"] if problems else None,
            "path": path,
            "missing": problems,
        }
    }
