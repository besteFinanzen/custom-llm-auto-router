# Implementation Plan — Failure Tracking & Retry Improvements

## Phase 1: Enhanced State Management (state.py)

### Current State Structure:
```python
self.data = {"cooldowns": {}, "usage": {}, "balances": {},
             "totals": {}, "by_preset": {}, "by_model": {}, "daily": {},
             "daily_history": {}}
```

### Enhanced State Structure (additive):
```python
self.data = {
    # existing
    "cooldowns": {}, "usage": {}, "balances": {},
    "totals": {}, "by_preset": {}, "by_model": {}, "daily": {}, "daily_history": {},
    
    # NEW: consecutive failure tracking
    "failures": {
        "models": {},   # model_id -> {"count": int, "first_failure": ts, "last_error": str, "status": int}
        "providers": {}, # provider_id -> {"count": int, "first_failure": ts, "last_error": str, "status": int}
    },
    "backoff": {},      # model_id -> {"until": ts, "base": s, "mult": s} (or legacy timestamp)
}
```

### New State Methods:
1. `record_model_failure(model_id, provider, error, status)` — increment consecutive failure count for model AND provider
2. `record_provider_failure(provider_id, error, status)` — increment provider-level consecutive failure counter
3. `record_model_success(model_id)` / `record_provider_success(pid)` — reset counters on success
4. `get_model_failures(model_id)` / `get_provider_failures(pid)` — read failure info
5. `is_model_failed(model_id, policy)` / `is_provider_failed(pid, policy)` — check thresholds
6. `set_backoff(model_id, seconds, multiplier)` — exponential backoff scheduling
7. `backoff_until(model_id)` — get until-timestamp
8. `clear_backoff(model_id)` — clear backoff state

## Phase 2: Gateway Enhancements (gateway.py)

1. In `proxy_call`:
   - Check failure status before attempting; raise `UpstreamFail` with `excluded=True`
   - On failure in FAILOVER_STATUSES: record model+provider failure, apply backoff cooldown (base * multiplier^n)
   - On transport error: record model+provider failure, apply base backoff
   - On success: reset failure counters + clear backoff

2. In chat handler failure loop:
   - After all candidates fail, distinguish between "all failed via upstream" vs "excluded after thresholds"
   - Return appropriate error codes/types

## Phase 3: Selector Enhancements (selector.py)

1. In `rank_candidates`: skip models/providers whose failure counters exceed thresholds (with reason strings for logging).

## Phase 4: Configuration (catalog.yaml)

Add under `policy`:
```yaml
failures:
  enabled: true
  model_threshold: 3
  provider_threshold: 5
  backoff_multiplier: 2.0
  backoff_base_seconds: 60
  backoff_max_seconds: 900
```

## Phase 5: Status + Error Responses

- `/v1/status`: include `failures` map + `backoff` map
- Error responses: use failure counts for final error message

## Phase 6: Tests

- Update/add tests for failure tracking, backoff, thresholds, error responses.