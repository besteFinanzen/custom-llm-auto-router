"""Standalone tracker application (``APP_MODE=tracker``)."""
from __future__ import annotations

import os
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse

from .api import page, router, waka
from .budgets import BudgetGate
from .config import Settings
from .store import TrackerStore

ROOT = pathlib.Path(__file__).resolve().parents[1]


def create_tracker_app(settings: Settings | None = None,
                       store: TrackerStore | None = None,
                       config_dir: str | pathlib.Path | None = None,
                       state_dir: str | pathlib.Path | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    config_dir = pathlib.Path(config_dir or os.environ.get("ROUTER_CONFIG_DIR") or ROOT)
    state_dir = pathlib.Path(state_dir or os.environ.get("ROUTER_STATE_DIR") or config_dir / "state")
    owns_store = store is None
    store = store or (TrackerStore(settings.database_url, settings)
                      if settings.database_url else None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if owns_store and app.state.wt_store is not None:
            app.state.wt_store.close()

    app = FastAPI(title="Worktracker", version="1.0.0", lifespan=lifespan,
                  docs_url="/worktracker/docs", openapi_url="/worktracker/openapi.json")
    app.state.wt_settings = settings
    app.state.wt_store = store
    app.state.wt_budget_gate = BudgetGate(store, settings) if store else None
    app.state.wt_config_dir = config_dir
    app.state.wt_state_dir = state_dir
    app.state.wt_runtime = None
    app.state.wt_app_mode = os.environ.get("APP_MODE", "tracker")

    app.include_router(page)
    app.include_router(router, prefix="/worktracker/api")
    app.include_router(waka, prefix="/api/v1")

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse(url="/worktracker")

    @app.get("/healthz")
    async def healthz():
        if app.state.wt_store is None:
            return JSONResponse({"status": "degraded", "database": False}, status_code=503)
        try:
            status = app.state.wt_store.status()
        except Exception as exc:
            return JSONResponse({"status": "error", "database": True, "error": str(exc)},
                                status_code=503)
        return {"status": "ok", "mode": app.state.wt_app_mode, "database": True, **status}

    return app


app = create_tracker_app()
