"""Load catalog.yaml (+ optional .env) into runtime config objects."""
from __future__ import annotations

import os
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent


def load_dotenv(path: pathlib.Path | None = None) -> None:
    """Populate os.environ from a KEY=VALUE file (no override of existing env)."""
    p = path or ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        if v:
            os.environ.setdefault(k.strip(), v)


class Provider:
    def __init__(self, pid: str, cfg: dict):
        self.id = pid
        self.base_url = cfg["base_url"].rstrip("/")
        self.api_key_env = cfg.get("api_key_env")
        self.api_key = os.environ.get(self.api_key_env) if self.api_key_env else None
        # explicit enabled:false wins (e.g. a key that is valid only inside the
        # vendor's own client); otherwise env unset => provider disabled
        if "enabled" in cfg:
            self.enabled = bool(cfg["enabled"])
        else:
            self.enabled = bool(self.api_key) if self.api_key_env else True
        self.balance = cfg.get("balance") or {"type": "none"}
        self.headers = cfg.get("headers") or {}
        self.discovery = cfg.get("discovery") or {}
        # optional: discovery URL when the OpenAI-compatible base differs from the
        # models-list endpoint (e.g. opencode.ai: /inference/openai/v1 vs /inference/v1)
        self.models_url = cfg.get("models_url")

    def auth_headers(self) -> dict:
        h = dict(self.headers)
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h


class Model:
    # Declared here so type checkers see the attributes the catalog YAML always
    # supplies; ``__init__`` still copies arbitrary extra keys via setattr.
    id: str
    provider: str
    upstream: str

    def __init__(self, cfg: dict):
        self.rank_override = None
        self.cost_override = None
        self.mdev_id = None
        self.aa_slug = None
        self.oc_id = None
        self.image = False
        self.output = 8192
        for k, v in cfg.items():
            setattr(self, k, v)
        self.enabled = cfg.get("enabled", True)
        self.cost_in = float(cfg.get("cost_in", 0))
        self.cost_out = float(cfg.get("cost_out", 0))
        self.seed_rank = float(cfg.get("seed_rank", 0))
        self.context = int(cfg.get("context", 32768))
        self.tools = bool(cfg.get("tools", True))


class Catalog:
    def __init__(self, raw: dict):
        self.policy = raw.get("policy") or {}
        self.blocklist = set(raw.get("blocklist") or [])
        self.providers = {pid: Provider(pid, p) for pid, p in (raw.get("providers") or {}).items()}
        self.presets = raw.get("presets") or {}
        self.models = {}
        for mcfg in raw.get("models") or []:
            m = Model(mcfg)
            self.models[m.id] = m
            if m.provider not in self.providers:
                raise ValueError(f"model {m.id}: unknown provider {m.provider}")

    @classmethod
    def load(cls, path: pathlib.Path | None = None) -> Catalog:
        return cls(yaml.safe_load((path or ROOT / "catalog.yaml").read_text()))

    def disabled_providers(self) -> list[str]:
        return [p.id for p in self.providers.values() if not p.enabled]
