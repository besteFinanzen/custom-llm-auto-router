"""Benchmark registry and normalisation.

Public sources are fetched best-effort and normalised to 0-100, keyed by a
normalised model name.  A failed source never blocks a refresh; the previous
snapshot is kept for that component.  Artificial Analysis stays in
``refresh.py`` and is optional (``AA_API_KEY``).
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import pathlib
import re
import tempfile
from collections.abc import Mapping
from typing import Any

import yaml

SOURCES: dict[str, dict[str, str]] = {
    "aider_polyglot": {
        "url": "https://raw.githubusercontent.com/Aider-AI/aider/main/aider/website/_data/polyglot_leaderboard.yml",
        "format": "aider",
    },
    "swebench": {
        "url": "https://raw.githubusercontent.com/SWE-bench/swe-bench.github.io/master/data/leaderboards.json",
        "format": "swebench",
    },
    "terminalbench": {
        "url": "https://raw.githubusercontent.com/harbor-framework/terminal-bench/main/leaderboard/leaderboard.yaml",
        "format": "terminalbench",
    },
    "bfcl": {
        "url": "https://gorilla.cs.berkeley.edu/data_overall.csv",
        "format": "bfcl",
    },
    "livebench": {
        "url": "https://raw.githubusercontent.com/LiveBench/LiveBench/main/data/table.csv",
        "format": "livebench",
    },
}


def normalize(name: str) -> str:
    lowered = str(name or "").strip().lower()
    lowered = re.sub(r"\s*\([^)]*\)\s*$", "", lowered)
    lowered = lowered.split("/")[-1]
    return re.sub(r"[^a-z0-9]+", "", lowered)


def _score(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if 0 <= number <= 1:
        number *= 100
    return round(max(0.0, min(100.0, number)), 2)


def parse_aider(text: str) -> dict[str, float]:
    data = yaml.safe_load(text) or []
    out: dict[str, float] = {}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, Mapping):
            continue
        name = item.get("model") or item.get("name")
        value = (item.get("pass_rate_2") or item.get("pass_rate")
                 or item.get("percent") or item.get("score"))
        score = _score(value)
        if name and score is not None:
            out[str(name)] = score
    return out


def parse_swebench(text: str) -> dict[str, float]:
    data = json.loads(text)
    out: dict[str, float] = {}
    boards = data.get("leaderboards", data) if isinstance(data, Mapping) else data
    if not isinstance(data, Mapping) and data and isinstance(data, list):
        # A direct list of results is another common exported format.
        boards = [{"results": data}]
    if isinstance(boards, Mapping):
        # SWE-bench has used both {results: [...]} and {verified: [...]}.
        # Keep supporting the advertised leaderboards.json payload shapes.
        boards = [boards] if "results" in boards else list(boards.values())
    for board in boards or []:
        for row in (board.get("results") if isinstance(board, Mapping) else board) or []:
            if not isinstance(row, Mapping):
                continue
            name = row.get("name") or row.get("model")
            score = _score(row.get("resolved") if row.get("resolved") is not None
                           else row.get("score"))
            if name and score is not None:
                out[str(name)] = max(out.get(str(name), 0.0), score)
    return out


def parse_terminalbench(text: str) -> dict[str, float]:
    data = yaml.safe_load(text) or []
    out: dict[str, float] = {}
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, Mapping):
            continue
        name = row.get("model") or row.get("agent")
        score = _score(row.get("accuracy") if row.get("accuracy") is not None
                       else row.get("score"))
        if name and score is not None:
            out[str(name)] = max(out.get(str(name), 0.0), score)
    return out


def parse_csv(text: str, preferred: tuple[str, ...] = ("overall", "average", "score")) -> dict[str, float]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return {}
    name_key = next((key for key in reader.fieldnames
                     if key.strip().lower() in {"model", "models", "name"}), None)
    if not name_key:
        return {}
    score_key = next((key for key in reader.fieldnames
                      if any(word in key.strip().lower() for word in preferred)), None)
    if not score_key:
        return {}
    out: dict[str, float] = {}
    for row in reader:
        name = row.get(name_key)
        score = _score(row.get(score_key))
        if name and score is not None:
            out[str(name)] = score
    return out


PARSERS = {
    "aider": parse_aider,
    "swebench": parse_swebench,
    "terminalbench": parse_terminalbench,
    "bfcl": parse_csv,
    "livebench": parse_csv,
}


def parse(fmt: str, text: str) -> dict[str, float]:
    parser = PARSERS.get(fmt)
    if not parser:
        raise ValueError(f"unknown benchmark format {fmt}")
    return parser(text)


def load(state_dir: str | pathlib.Path) -> dict:
    path = pathlib.Path(state_dir) / "benchmarks.json"
    if not path.exists():
        return {"fetched_at": None, "sources": {}, "models": {}}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {"fetched_at": None, "sources": {}, "models": {}}
    data.setdefault("sources", {})
    data.setdefault("models", {})
    return data


def refresh(config_dir: str | pathlib.Path, state_dir: str | pathlib.Path,
            client: Any = None, sources: Mapping[str, Mapping] | None = None) -> dict:
    """Fetch every source, merge into ``state/benchmarks.json`` atomically."""
    import httpx

    state_dir = pathlib.Path(state_dir)
    previous = load(state_dir)
    merged: dict[str, dict[str, float]] = {
        name: dict(values) for name, values in previous.get("models", {}).items()}
    report: dict[str, dict] = {}
    own_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True,
                                    headers={"user-agent": "worktracker/1.0"})
    try:
        for source, cfg in (sources or SOURCES).items():
            fmt = str(cfg.get("format") or source)
            try:
                response = client.get(cfg["url"])
                response.raise_for_status()
                scores = parse(fmt, response.text)
                for model, score in scores.items():
                    merged.setdefault(model, {})[source] = score
                report[source] = {"status": "ok", "models": len(scores)}
            except Exception as exc:
                report[source] = {"status": "error", "error": str(exc)[:300]}
    finally:
        if own_client:
            client.close()
    data = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "sources": report,
        "models": merged,
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".benchmarks.", suffix=".json")
    with open(fd, "w") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
    pathlib.Path(tmp).replace(state_dir / "benchmarks.json")
    return data


def components(model_id: str, data: Mapping) -> dict[str, float]:
    """Best benchmark components for a model id (exact, then suffix match)."""
    models = data.get("models") or {}
    if model_id in models:
        return dict(models[model_id])
    want = normalize(model_id)
    if not want:
        return {}
    for name, values in models.items():
        if normalize(name) == want:
            return dict(values)
    for name, values in models.items():
        norm = normalize(name)
        if norm and (norm.endswith(want) or want.endswith(norm)):
            return dict(values)
    return {}
