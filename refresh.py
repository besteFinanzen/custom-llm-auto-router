"""Refresh model ranks/costs from public data + discover provider models.

Sources (all best-effort; component-level merge with previous snapshot):
  1. Artificial Analysis REST (needs AA_API_KEY; free tier 100 req/day, 1 call suffices)
     -> aa_intelligence / aa_coding / aa_agentic
     matched via explicit aa_slug, else fuzzy name matching (matching.py)
  2. https://opencode.ai/data/index.json (no key, daily)
     -> oc_retention (weekly retention %) / oc_popularity (usage share, top = 100)
     matched via explicit oc_id, else fuzzy name matching
  3. https://models.dev/api.json (no key)
     -> data.cost_in/cost_out/context/output/tools (exact id, mdev_id, ':free' variants)
  4. Provider discovery: GET {base}/models -> state/discovered.json (discovery.py)
     discovered models are enriched by 1-3 above and become routing candidates.

Usage: python refresh.py [--state-dir state] [--config-dir .] [--no-discovery]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import tempfile

import httpx

from catalog import Catalog, load_dotenv
from discovery import load_discovered, sync_discovered
from matching import best_match

AA_URL = "https://artificialanalysis.ai/api/v2/language/models/free"
OC_URL = "https://opencode.ai/data/index.json"
MODELS_DEV_URL = "https://models.dev/api.json"
MODELS_DEV_PROVIDERS = {"openrouter": "openrouter", "deepseek": "deepseek",
                        "opencode": "opencode", "google": "google"}
AA_FIELDS = [("artificial_analysis_intelligence_index", "aa_intelligence"),
             ("artificial_analysis_coding_index", "aa_coding"),
             ("artificial_analysis_agentic_index", "aa_agentic")]
REVIEW_HOWS = {"family", "similar-ambiguous", "base-ambiguous", "exact-ambiguous"}


def fetch_json(client: httpx.Client, url: str, headers: dict | None = None):
    try:
        r = client.get(url, headers=headers or {})
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        print(f"  ! fetch failed {url}: {str(exc)[:150]}")
        return None


def clamp100(x: float) -> float:
    return max(0.0, min(100.0, float(x)))


def aa_records(aa) -> tuple[dict[str, dict], dict[str, list[str]], dict[str, float]]:
    """Return (slug -> raw record, slug -> alias names, slug -> quality)."""
    records: dict[str, dict] = {}
    idx: dict[str, list[str]] = {}
    qual: dict[str, float] = {}
    items = aa.get("data") if isinstance(aa, dict) else aa
    for it in items or []:
        if not isinstance(it, dict):
            continue
        slug = it.get("slug") or it.get("id")
        if not slug:
            continue
        records[slug] = it
        names = [slug] + ([str(it["name"])] if it.get("name") else [])
        idx[slug] = names
        v = it.get("artificial_analysis_intelligence_index")
        if v is None:
            v = it.get("artificial_analysis_coding_index")
        qual[slug] = float(v) if v is not None else 0.0
    return records, idx, qual


def oc_indexes(oc: dict) -> tuple[dict[str, list[str]], dict[str, float], dict[str, float]]:
    idx: dict[str, list[str]] = {}
    ret: dict[str, float] = {}
    pop: dict[str, float] = {}
    if not isinstance(oc, dict):
        return idx, ret, pop
    wr = oc.get("weeklyRetention")
    if isinstance(wr, list):
        for e in wr:
            if isinstance(e, dict) and e.get("model") and e.get("retentionPercent") is not None:
                ret[str(e["model"])] = clamp100(e["retentionPercent"])
    elif isinstance(wr, dict):
        for mid, d in wr.items():
            v = d.get("retention") if isinstance(d, dict) else d
            if v is not None:
                ret[str(mid)] = clamp100(v)
    lb = (oc.get("leaderboard") or {}).get("pastWeek") or []
    toks = {str(e.get("model") or e.get("id")): float(e.get("tokens") or 0)
            for e in lb if isinstance(e, dict) and (e.get("model") or e.get("id"))}
    mx = max(toks.values(), default=0)
    if mx:
        pop = {mid: clamp100(t / mx * 100) for mid, t in toks.items()}
    for mid in set(ret) | set(pop):
        idx[mid] = [mid]
    return idx, ret, pop


def mdev_record(mdev, provider: str, model) -> dict | None:
    prov_key = MODELS_DEV_PROVIDERS.get(provider)
    if not (isinstance(mdev, dict) and prov_key):
        return None
    prov_models = ((mdev.get(prov_key) or {}).get("models")) or {}
    mid = getattr(model, "mdev_id", None) or model.upstream
    if mid.endswith(":free"):
        # Never fall back to the paid base model for a ':free' variant: that is
        # how a free model ends up with a non-zero price and fails a $0 preset.
        return prov_models.get(mid)
    return prov_models.get(mid) or prov_models.get(mid + ":free")


def match_model(model, aa_recs, aa_i, aa_q, oc_i, oc_r) -> tuple[dict, dict[str, dict]]:
    """Return (components, match audit) for one model."""
    comps: dict[str, float] = {}
    audit: dict[str, dict] = {}

    # ---- Artificial Analysis
    slug = getattr(model, "aa_slug", None)
    if slug:
        if slug in aa_recs:
            audit["aa"] = {"slug": slug, "confidence": 1.0, "how": "explicit"}
        else:
            audit["aa"] = {"slug": slug, "confidence": 1.0, "how": "explicit-missing"}
            slug = None
    else:
        slug, conf, how, alts = best_match(model.upstream, aa_i, aa_q)
        if not slug and model.id != model.upstream:
            slug, conf, how, alts = best_match(model.id, aa_i, aa_q)
        audit["aa"] = ({"slug": slug, "confidence": conf, "how": how,
                        "alternatives": alts[:3]} if slug else {"how": "none"})
    if slug:
        it = aa_recs[slug]
        for src, dst in AA_FIELDS:
            if it.get(src) is not None:
                comps[dst] = clamp100(it[src])

    # ---- opencode.ai/data
    oc_id = getattr(model, "oc_id", None)
    if oc_id:
        audit["oc"] = ({"id": oc_id, "confidence": 1.0, "how": "explicit"}
                       if oc_id in oc_i else {"id": oc_id, "how": "explicit-missing"})
    else:
        oc_id, conf, how, alts = best_match(model.upstream, oc_i, oc_r)
        audit["oc"] = ({"id": oc_id, "confidence": conf, "how": how,
                        "alternatives": alts[:3]} if oc_id else {"how": "none"})
    if oc_id and oc_id in oc_r:
        comps["oc_retention"] = oc_r[oc_id]
    return comps, audit


def compose(catalog: Catalog, models: dict, aa, oc, mdev, old_models: dict) -> dict:
    """Pure composition of the ranks snapshot. Returns the ranks.json payload."""
    aa_recs, aa_i, aa_q = aa_records(aa)
    oc_i, oc_r, oc_p = oc_indexes(oc)

    out_models: dict[str, dict] = {}
    unmatched: list[str] = []
    review: list[dict] = []

    for m in models.values():
        prev = old_models.get(m.id) or {}
        comps = {k: v for k, v in prev.items() if k.startswith(("aa_", "oc_"))}
        data = dict(prev.get("data") or {})

        if aa_recs:
            aa_comps, audit = match_model(m, aa_recs, aa_i, aa_q, {}, {})
            comps.update(aa_comps)
            audit_aa = audit["aa"]
        else:
            audit_aa = {"how": "skipped"}

        if oc_i:
            # only OC part of the audit is needed here
            _, audit = match_model(m, {}, {}, {}, oc_i, oc_r)
            audit_oc = audit["oc"]
            oc_id = None if audit_oc.get("how", "").endswith("missing") else audit_oc.get("id")
            if oc_id in oc_r:
                comps["oc_retention"] = oc_r[oc_id]
            if oc_id in oc_p:
                comps["oc_popularity"] = oc_p[oc_id]
        else:
            audit_oc = {"how": "skipped"}

        rec = mdev_record(mdev, m.provider, m)
        if rec:
            cost = rec.get("cost") or {}
            limit = rec.get("limit") or {}
            if cost.get("input") is not None:
                data["cost_in"] = float(cost["input"])
            if cost.get("output") is not None:
                data["cost_out"] = float(cost["output"])
            if limit.get("context"):
                data["context"] = int(limit["context"])
            if limit.get("output"):
                data["output"] = int(limit["output"])
            if rec.get("tool_call") is not None:
                data["tools"] = bool(rec["tool_call"])

        # A ':free' variant is free by definition. Guard against stale/paid
        # models.dev data (or an old snapshot) re-introducing a price.
        if str(getattr(m, "upstream", "")).endswith(":free"):
            data["cost_in"] = 0.0
            data["cost_out"] = 0.0

        entry = dict(comps)
        if data:
            entry["data"] = data
        entry["match"] = {"aa": audit_aa, "oc": audit_oc}
        out_models[m.id] = entry

        if not any(k.startswith(("aa_", "oc_")) for k in comps):
            unmatched.append(m.id)
        for src, a in (("aa", audit_aa), ("oc", audit_oc)):
            if a.get("how") in REVIEW_HOWS or (a.get("confidence", 1.0) < 0.9 and
                                               (a.get("slug") or a.get("id"))):
                review.append({"id": m.id, "source": src,
                               "matched": a.get("slug") or a.get("id"),
                               "confidence": a.get("confidence"),
                               "how": a.get("how"),
                               "alternatives": a.get("alternatives", [])})

    return {
        "version": 2,
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "models": out_models,
        "unmatched": unmatched,
        "review": review,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-dir", default=pathlib.Path(__file__).resolve().parent)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--no-discovery", action="store_true")
    args = ap.parse_args()
    cdir = pathlib.Path(args.config_dir)
    sdir = pathlib.Path(args.state_dir or cdir / "state")
    sdir.mkdir(parents=True, exist_ok=True)

    load_dotenv(cdir / ".env")
    catalog = Catalog.load(cdir / "catalog.yaml")
    old_path = sdir / "ranks.json"
    old = json.loads(old_path.read_text()) if old_path.exists() else {}
    old_models = old.get("models") or {}

    with httpx.Client(timeout=30.0) as client:
        aa_key = os.environ.get("AA_API_KEY")
        aa = fetch_json(client, AA_URL, {"x-api-key": aa_key}) if aa_key else None
        if not aa:
            print("  - Artificial Analysis skipped (no AA_API_KEY or fetch failed)")
        oc = fetch_json(client, OC_URL)
        mdev = fetch_json(client, MODELS_DEV_URL)

        if not args.no_discovery and (catalog.policy.get("discovery") or {}).get("enabled", True):
            sync = sync_discovered(catalog, sdir, client)
            for pid, st in (sync.get("providers") or {}).items():
                if st.get("ok"):
                    print(f"  · discovered {pid}: listed={st['listed']} used={st['used']}")
                else:
                    print(f"  ! discovery {pid} failed ({st.get('error')})")
        else:
            print("  - discovery skipped")

    discovered = load_discovered(sdir, catalog)
    models_all: dict = {**discovered, **catalog.models}  # curated wins
    print(f"  · candidate models: {len(models_all)} "
          f"({len(catalog.models)} curated + {len(models_all) - len(catalog.models)} discovered)")

    out = compose(catalog, models_all, aa, oc, mdev, old_models)

    fd, tmp = tempfile.mkstemp(dir=sdir, prefix=".ranks-", suffix=".tmp")
    with open(fd, "w") as f:
        json.dump(out, f, indent=1)
    pathlib.Path(tmp).replace(old_path)

    nm = out["models"]
    print(f"\nranks.json written ({len(nm)} models, {len(out['unmatched'])} without quality data)")
    for mid, comps in nm.items():
        tags = " ".join(f"{k.split('_', 1)[1][:4]}={v:.0f}" for k, v in comps.items()
                        if k.startswith(("aa_", "oc_"))) or "(seed prior only)"
        print(f"  {mid:48s} {tags}")
    if out["review"]:
        print("\nfuzzy matches to review (pin aa_slug/oc_id in catalog.yaml if wrong):")
        for r in out["review"]:
            print(f"  {r['id']:40s} {r['source']}->{r['matched']} "
                  f"({r['how']}, conf {r['confidence'] or 0:.2f}) alts={r['alternatives'][:2]}")
    if out["unmatched"]:
        print("\nno quality data (seed prior in use):")
        for mid in out["unmatched"]:
            print(f"  - {mid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
