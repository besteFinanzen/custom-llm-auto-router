"""Fuzzy name matching between catalog models and external data sources
(Artificial Analysis slugs, opencode.ai/data ids).

Tiers (first hit wins):
  1. exact alias            confidence 1.00
  2. base-name exact        confidence 0.95   (vendor prefix ignored)
  3. similarity >= 0.86     confidence = ratio
  4. family/prefix match    confidence 0.60   (e.g. alias "kimi" -> best "kimi-*";
                                                picked by quality, review logged)
Everything else -> no match (seed prior stays). Low-confidence matches are
reported with alternatives so they can be pinned via aa_slug/oc_id in catalog.yaml.
"""
from __future__ import annotations

import difflib
import re

SIMILARITY_THRESHOLD = 0.86
FAMILY_CONFIDENCE = 0.60


def normalize(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r":(free|beta|alpha|preview)$", "", s)
    s = re.sub(r"-(free|preview|latest)$", "", s)
    s = re.sub(r"[_.\s]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


def base_name(name: str) -> str:
    return normalize(name.rsplit("/", 1)[-1])


def aliases_of(name: str) -> list[str]:
    """Names to compare against: full slug and vendor-stripped base."""
    n = normalize(name)
    out = [n]
    b = base_name(name)
    if b != n:
        out.append(b)
    return out


def _similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split("-")), set(b.split("-"))
    if ta and tb:
        ratio = max(ratio, len(ta & tb) / len(ta | tb))
    return ratio


def best_match(target: str, index: dict[str, list[str]],
               quality: dict[str, float] | None = None) -> tuple[str | None, float, str, list[str]]:
    """Match `target` against {key: [alias strings]}.

    `quality` (key -> 0..100) is used to pick the best family match.
    Returns (key, confidence, how, alternatives).
    """
    qual = quality or {}
    targets = set(aliases_of(target))

    exact = []
    for key, als in index.items():
        norm_als = {normalize(a) for a in als} | {normalize(key)}
        if targets & norm_als:
            exact.append(key)
    if len(exact) == 1:
        return exact[0], 1.0, "exact", []
    if len(exact) > 1:  # ambiguous exact (e.g. same model under two vendors)
        best = max(exact, key=lambda k: qual.get(k, 0))
        return best, 0.9, "exact-ambiguous", sorted(exact)

    # base-name exact
    tb = {base_name(target)}
    base_hits = [k for k in index
                 if base_name(k) in tb or any(base_name(a) in tb for a in index[k])]
    if len(base_hits) == 1:
        return base_hits[0], 0.95, "base", []
    if len(base_hits) > 1:
        best = max(base_hits, key=lambda k: qual.get(k, 0))
        return best, 0.9, "base-ambiguous", sorted(base_hits)

    # similarity
    scored: list[tuple[float, str]] = []
    for key, als in index.items():
        cand = [normalize(key)] + [base_name(a) for a in als]
        s = max(_similarity(t, c) for t in targets for c in cand)
        scored.append((s, key))
    scored.sort(reverse=True)
    if scored and scored[0][0] >= SIMILARITY_THRESHOLD:
        best_s, best_k = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        alts = [k for s, k in scored[1:4] if s >= SIMILARITY_THRESHOLD]
        if alts and (best_s - second) < 0.03:
            return best_k, round(best_s, 3), "similar-ambiguous", sorted(alts + [best_k])
        return best_k, round(best_s, 3), "similar", alts

    # family: target is a token-prefix of longer names ("kimi" -> "kimi-k2-5")
    family = []
    prefix = next(iter(tb))
    for key, als in index.items():
        for cand in [normalize(key)] + [base_name(a) for a in als]:
            if cand.startswith(prefix + "-"):
                family.append(key)
                break
    family = sorted(set(family))
    if family:
        best = max(family, key=lambda k: qual.get(k, 0))
        return best, FAMILY_CONFIDENCE, "family", family

    return None, 0.0, "none", []
