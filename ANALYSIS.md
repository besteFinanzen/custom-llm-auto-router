# Current Retry Mechanism Analysis

## 1. Current Failure Handling Overview

### gateway.py
- **FAILOVER_STATUSES**: {408, 409, 429, 500, 502, 503, 504} - HTTP codes that trigger failover
- **UpstreamFail exception**: Tracks failed upstream calls with status and error details
- **Current flow**:
  1. Try model (max_failovers = 3)
  2. If failure in FAILOVER_STATUSES: set cooldown (60-900s from config)
  3. If transport error: set cooldown (60s from config)
  4. Log decision with tried/rejected models
  5. Return error 502 if all fail

### selector.py
- **rank_candidates**: Filters eligible models before trying them
- **Rejection reasons**: blocklisted, disabled, provider disabled, over cost ceiling, context too small, no tools, cooling down, balance exhausted

### state.py
- **cooldowns**: Time-based exclusion (timestamp-based)
- **balances**: Per-provider balance status
- **usage**: Token usage tracking
- **No failure counters**: Only time-based cooldowns

### catalog.yaml
- **Policy config**: cooldown_seconds, cooldown_max_seconds, max_failovers
- **No per-model/per-provider thresholds**: Only global cooldown settings

## 2. Current Limitations

Based on user's request for:
- **Per-model failure counters** ❌ Not implemented - only time-based cooldowns
- **Per-provider failure counters** ❌ Not implemented
- **Switching when consecutive failures exceed thresholds** ❌ No consecutive failure tracking
- **Exponential backoff** ❌ Static cooldowns
- **Preventing repeated failed attempts** ❌ Only time-based prevention
- **Error responses when all options fail** ❌ Basic 502 error

## 3. Proposed Design

### Enhanced State Structure
```python
{
  "cooldowns": {"model_id": timestamp},  # existing
  "failures": {                         # NEW
    "models": {"model_id": {
      "count": 5,                        # consecutive failures
      "first_failure": timestamp,       # when first failure occurred
      "provider": "provider_id",        # track provider for backoff
      "last_error": "error details"
    }},
    "providers": {"provider_id": {
      "count": 5,                        # consecutive failures
      "first_failure": timestamp,
      "last_error": "error details"
    }
  },
  "backoff": {                          # NEW - exponential backoff tracking
    "model_id": {"base": 60, "multiplier": 2, "next": timestamp}
  },
  ... // existing fields (usage, balances, etc.)
}
```

### Configuration Enhancements (catalog.yaml)
```yaml
policy:
  # existing
  cooldown_seconds: 60
  cooldown_max_seconds: 900
  max_failovers: 3

  # NEW - per-model failure thresholds
  model_failure_threshold: 3          # consecutive model failures before exclusion
  model_failure_window_seconds: 300   # time window for consecutive failures (optional)

  # NEW - per-provider failure thresholds  
  provider_failure_threshold: 5        # consecutive provider failures before exclusion
  provider_failure_window_seconds: 300 # time window for consecutive failures

  # NEW - exponential backoff
  backoff_enabled: true
  backoff_multiplier: 2               # how much to increase each failure
  backoff_base_seconds: 60            # base cooldown
  backoff_max_seconds: 900           # max cooldown (capped)

  # NEW - error response configuration
  error_response_when_all_fail: "502"  # or "503" for budget-only
  error_response_when_all_providers_fail: "503"  # provider-level failure
```

### Core Changes Required

#### 1. Enhanced State Class (state.py)
- Add `failures` and `backoff` dictionaries
- Add methods for failure tracking:
  - `record_model_failure(model_id, provider, error)`
  - `record_provider_failure(provider_id, error)`
  - `get_model_failures(model_id)`
  - `get_provider_failures(provider_id)`
  - `clear_failures(model_id, provider_id)`
- Add success reset methods
- Add provider-level cooldown support

#### 2. Enhanced Gateway Logic (gateway.py)
- **proxy_call**: Enhanced to track failures and implement exponential backoff
- **rank_candidates**: Extended to filter out failed models/providers
- **Error responses**: More informative error responses

#### 3. Enhanced Selector Logic (selector.py)
- Add filtering for consecutive failures exceeding thresholds
- Add provider-level failure checks
- Integrate with new state failure tracking

## 4. Implementation Priorities

### Phase 1: Core Failure Tracking (Week 1)
1. Add failure tracking to State class
2. Modify proxy_call to record failures on UpstreamFail
3. Add provider-level failure tracking
4. Update selector to respect failure thresholds

### Phase 2: Exponential Backoff (Week 2)  
1. Add backoff calculation logic
2. Modify cooldown setting to use backoff values
3. Add backoff reset on success
4. Ensure cooldowns still work for non-failure cases

### Phase 3: Enhanced Error Handling (Week 3)
1. Improve error responses with detailed failure info
2. Add different error codes for different failure scenarios
3. Update decision logging with failure counts
4. Add status endpoint visibility for failures

### Phase 4: Testing and Validation (Week 4)
1. Write comprehensive tests for new failure tracking
2. Test exponential backoff behavior
3. Validate provider-level failure handling
4. Ensure backward compatibility

## 5. Backward Compatibility

- All existing config works unchanged
- New fields are optional (have sensible defaults)
- Existing cooldown mechanism preserved
- New failure tracking is additive, not replacing existing logic
- Tests updated to verify new behavior while maintaining existing test expectations

## 6. Testing Strategy

### New Tests to Add:
- Test per-model failure counters and threshold enforcement
- Test per-provider failure counters
- Test exponential backoff progression
- Test failure persistence across requests
- Test success resets failure counters
- Test enhanced error responses
- Test selector filtering based on failures
- Integration tests with real providers

### Existing Tests Modified:
- Update cooldown tests to verify new failure tracking
- Update error response tests for enhanced error messages
- Update selector tests for new failure-based filtering
- All tests should pass with new implementation

## 7. Benefits

1. **More robust failure recovery**: Prevents repeated attempts on permanently broken models/providers
2. **Intelligent backoff**: Reduces load on struggling endpoints
3. **Better error reporting**: Users get actionable failure information
4. **Configurable thresholds**: Different environments can tune failure behavior
5. **Dual-level tracking**: Both model and provider failure tracking
6. **Exponential backoff**: Progressive cooldowns for persistent failures
7. **Prevention of repeated failures**: Hard limits on consecutive failures