"""Session-title resolution: OpenCode session ID -> human-readable title.

Titles are read from the local OpenCode background service via
`opencode api get /api/session` (the API authenticates/discovers itself) and
cached persistently in ``state/titles.json``. Refreshes are triggered lazily by
``GET /v1/spend`` when unknown session IDs appear, at most once per
``REFRESH_INTERVAL``, in a background task so the 2 s dashboard poll never
blocks. A fresh session has a title only after OpenCode generates it (the aux
``title`` call — which itself routes through this gateway), so titles trickle
in on subsequent refreshes.

Disable entirely by setting ``ROUTER_TITLES_CMD`` to an empty string; override
to point at a different command (default: ``opencode api get /api/session``).
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import pathlib
import shlex
import shutil
import time

REFRESH_INTERVAL = 60.0
SUBPROCESS_TIMEOUT = 15.0
DEFAULT_CMD = "opencode api get /api/session"


def parse_sessions(payload: str) -> dict[str, str]:
    """Parse `opencode api get /api/session` output into {session_id: title}.

    Accepts both the wrapped {"data": [...]} shape and a bare list.
    """
    try:
        data = json.loads(payload)
    except ValueError:
        return {}
    if isinstance(data, dict):
        data = data.get("data")
    if not isinstance(data, list):
        return {}
    out: dict[str, str] = {}
    for s in data:
        if isinstance(s, dict) and s.get("id") and s.get("title"):
            out[str(s["id"])] = str(s["title"])
    return out


def title_cmd() -> list[str] | None:
    """Configured refresh command, or None when titles are disabled/missing."""
    raw = os.environ.get("ROUTER_TITLES_CMD", DEFAULT_CMD)
    if not raw.strip():
        return None
    cmd = shlex.split(raw)
    return cmd if shutil.which(cmd[0]) else None


class TitleCache:
    """Persistent id->title cache with throttled background refresh."""

    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self.titles: dict[str, str] = {}
        self.last_attempt = 0.0
        self._task: asyncio.Task | None = None
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text())
                self.titles = {str(k): str(v) for k, v in (raw.get("titles") or {}).items()}
            except Exception:
                self.titles = {}

    def lookup(self, session_id: str | None) -> str | None:
        if not session_id or session_id == "(unknown)":
            return None
        return self.titles.get(session_id)

    def ensure_refresh(self, unknown_ids: list[str]) -> None:
        """Kick a throttled background refresh for unknown ids (non-blocking)."""
        if not unknown_ids or title_cmd() is None:
            return
        now = time.time()
        if now - self.last_attempt < REFRESH_INTERVAL:
            return
        if self._task and not self._task.done():
            return
        self.last_attempt = now
        self._task = asyncio.create_task(self._refresh())

    async def _refresh(self) -> None:
        cmd = title_cmd()
        if not cmd:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=SUBPROCESS_TIMEOUT)
            if proc.returncode != 0:
                return
            found = parse_sessions(out.decode("utf-8", "replace"))
            if not found:
                return
            self.titles.update(found)
            self.path.write_text(json.dumps({
                "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "titles": self.titles}, indent=1, sort_keys=True))
        except Exception:
            return  # enrichment is best-effort; never break the spend endpoint
