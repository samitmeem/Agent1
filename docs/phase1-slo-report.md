# Phase 1 — SLO Measurements Report

Date: 2026-07-30
Environment: Python 3.11.15 (uv venv `.venv-test`)

## Test Suite Results

| Suite | Tests | Passed | Skipped | Duration |
|-------|-------|--------|---------|----------|
| worker/tests/test_integration.py | 26 | 25 | 1 | ~5 s |
| Full repo (worker, gateway, llm, voice, observability, contracts) | 227 | 226 | 1 | ~63 s |

## Latency SLO Harness (mocked Qwen/vLLM — deterministic)

Measured over a full graph run: load_context → classify → plan → model → compose → persist → complete, with a streaming chunk emitted during compose.

| Metric | Target | Measured | Status |
|--------|--------|----------|--------|
| ACK latency (message accepted → ack) | < 300 ms | ≈ 0 ms | ✅ |
| First `agent:status_update` | < 1 s | < 5 ms | ✅ |
| First `response:chunk` | < 2 s | < 5 ms | ✅ |

Mocked runtimes are near-zero because the LLM router, tools, and persistence are in-process mocks. The harness validates the measurement plumbing and event ordering; absolute numbers against real services are validated in Phase 2 (live-mode variant, `SLO_LIVE=1`, currently skipped).

## Resiliency Scenarios Verified

| Scenario | Test | Status |
|----------|------|--------|
| Concurrent inputs — lock prevents parallel runs | TestConcurrentInputs (2 tests) | ✅ |
| Worker crash/restart — lock reclaimable after lease expiry | TestWorkerCrashRestart (2 tests) | ✅ |
| Resume from last LangGraph checkpoint after crash | TestCrashResumeFromCheckpoint | ✅ |
| Lock renewal extends lease; renewal fails on lost ownership | TestLockExpiry (3 tests) | ✅ |
| Lost ownership aborts run (LockNotOwnedError) | TestLockTakeoverAndAbort (2 tests) | ✅ |
| Second job takes over after lease lapse | TestLockTakeoverAndAbort | ✅ |
| Interrupted tool execution — retry reuses execution_id, no duplicate side effects | TestInterruptedToolIdempotency | ✅ |
| Tool error produces error response; graph still completes | TestInterruptedToolExecution (2 tests) | ✅ |
| STT/TTS outage — text ticket completes | TestVoiceOutageTextCompletion, TestVoiceOutageSLO (2 tests) | ✅ |
| SLO events recorded in order across pipeline | TestFullPipelineSLO | ✅ |
| Trace propagation (correlation_id, spans) | TestTracePropagation (4 tests) | ✅ |

## Production Code Changes

None required — all scenarios passed without production code fixes.

## Notes

- Test environment created via `uv`: `.venv-test` with Python 3.11.15 (project requires ≥3.11 for `enum.StrEnum`; system Python is 3.9).
- Run tests with: `.venv-test/bin/python -m pytest worker gateway llm voice observability contracts -q`
