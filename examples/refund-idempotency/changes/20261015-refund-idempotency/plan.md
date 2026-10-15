---
id: 20261015-refund-idempotency
title: Idempotency keys for refunds
status: draft
spec_sha: "87be3286340d5c4dd7bff984e1757bd5b0130a686a3246b5201788597d35c8cb"
intent_sha: "353d6ad533af6c74a2bcde6c640a237224a3ae4b005064acde5fd160580a3b06"
---
# Plan: Idempotency keys for refunds

## Goal
Refund retries with the same `Idempotency-Key` create at most one refund (FR-001 to FR-006, SC-001, SC-003).

## Files that change
- `migrations/0042_refund_idempotency.sql` (new)
- `payments/refunds/handler.py`
- `payments/refunds/idempotency.py` (new)
- `payments/jobs/purge_idempotency.py` (new)
- `tests/test_refund_idempotency.py` (new)

## Order of work
- [ ] T1 Write failing tests for replay, mismatch, expiry and no-key behaviour (FR-001, FR-003, FR-005, FR-006)
- [ ] T2 Add the migration with the unique index (FR-002)
- [ ] T3 Implement `idempotency.py` and wire it into the handler (FR-001, FR-002, FR-003, FR-006)
- [ ] T4 Add a concurrency test with 100 parallel requests and make it pass (FR-004, SC-001)
- [ ] T5 [P] Nightly purge job for rows older than 24 hours (FR-005)
- [ ] T6 [P] Benchmark requests without a key before and after (SC-003)

## Risks
The insert-first approach holds a row lock while the refund is processed, so a slow downstream call blocks retries. The handler therefore answers 409 after 2 seconds instead of waiting. Rejected: a Redis lock, which adds a dependency and a second source of truth.

## Proof
`make test` (includes `tests/test_refund_idempotency.py`); `python3 -m pytest tests/test_refund_idempotency.py::test_100_concurrent_requests_create_one_refund`; `make bench-refunds`. SC-002 is checked after release from support's monthly duplicate-refund count.

## Rollback
Revert the handler commit. The new table is unused without it and can stay; drop it in a later migration.

## Review focus
The unique-violation path in `idempotency.py`: that the replay never returns another merchant's response, and that the 409 path never creates a refund.

## Change log
