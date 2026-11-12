"""Automatic model/cost/benchmark refresh and the background scheduler."""
from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import subprocess
import sys
from typing import Any

from . import benchmarks

INTERVAL_HOURS = 24


def refresh_models(config_dir: str | pathlib.Path, state_dir: str | pathlib.Path,
                   timeout: int = 600) -> dict:
    """Run the existing refresh.py pipeline in a subprocess."""
    config_dir = pathlib.Path(config_dir)
    state_dir = pathlib.Path(state_dir)
    script = config_dir / "refresh.py"
    if not script.exists():
        return {"status": "error", "error": f"{script} not found"}
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--config-dir", str(config_dir),
             "--state-dir", str(state_dir)],
            cwd=str(config_dir), capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:500]}
    tail = (proc.stdout or "")[-2000:]
    return {"status": "ok" if proc.returncode == 0 else "error",
            "returncode": proc.returncode,
            "stdout": tail,
            "stderr": (proc.stderr or "")[-2000:]}


def refresh_all(config_dir: str | pathlib.Path, state_dir: str | pathlib.Path,
                store: Any = None, client: Any = None,
                include_models: bool = True) -> dict:
    """Refresh models, costs and every benchmark source; record run rows."""
    result: dict[str, Any] = {"started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    model_run = store.start_refresh("models") if store and include_models else None
    if include_models:
        models = refresh_models(config_dir, state_dir)
        result["models"] = models
        if store and model_run is not None:
            store.finish_refresh(model_run, models.get("status", "error"),
                                 models, models.get("error"))
    bench_run = store.start_refresh("benchmarks") if store else None
    try:
        data = benchmarks.refresh(config_dir, state_dir, client=client)
        ok = sum(1 for item in data["sources"].values() if item.get("status") == "ok")
        result["benchmarks"] = {
            "status": "ok" if ok else "error",
            "sources": data["sources"],
            "models": len(data.get("models") or {}),
            "fetched_at": data.get("fetched_at"),
        }
        if store and bench_run is not None:
            store.finish_refresh(bench_run, "ok" if ok else "error", result["benchmarks"])
    except Exception as exc:
        result["benchmarks"] = {"status": "error", "error": str(exc)[:500]}
        if store and bench_run is not None:
            store.finish_refresh(bench_run, "error", None, str(exc)[:500])
    result["ok"] = (result.get("models", {}).get("status") in {None, "ok"}
                    and result["benchmarks"].get("status") == "ok")
    return result


def _due(store: Any, interval_s: float) -> bool:
    if store is None:
        return True
    try:
        status = store.refresh_status()
    except Exception:
        return True
    last = status.get("last_success")
    if not last or not last.get("finished_at"):
        return True
    try:
        finished = dt.datetime.fromisoformat(str(last["finished_at"]).replace("Z", "+00:00"))
    except ValueError:
        return True
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - finished).total_seconds() >= interval_s


async def scheduler_loop(store: Any, config_dir: str | pathlib.Path,
                         state_dir: str | pathlib.Path,
                         interval_hours: float = INTERVAL_HOURS,
                         initial_delay: float = 15.0) -> None:
    interval_s = max(60.0, float(interval_hours) * 3600)
    await asyncio.sleep(initial_delay)
    while True:
        try:
            if _due(store, interval_s):
                await asyncio.to_thread(refresh_all, config_dir, state_dir, store)
        except Exception:
            pass
        await asyncio.sleep(min(interval_s, 3600.0))


def start_scheduler(app: Any) -> asyncio.Task | None:
    import os

    if (os.environ.get("TRACKER_REFRESH_ENABLED", "1").strip().lower()
            in {"0", "false", "no", "off"}):
        return None
    try:
        hours = float(os.environ.get("TRACKER_REFRESH_HOURS", str(INTERVAL_HOURS)))
    except ValueError:
        hours = INTERVAL_HOURS
    initial_delay = float(os.environ.get("TRACKER_REFRESH_INITIAL_DELAY", "15"))
    return asyncio.create_task(scheduler_loop(
        app.state.wt_store, app.state.wt_config_dir, app.state.wt_state_dir, hours,
        initial_delay))
