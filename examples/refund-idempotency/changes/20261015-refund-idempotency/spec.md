---
id: 20261015-refund-idempotency
title: Idempotency keys for refunds
status: draft
intent_sha: "353d6ad533af6c74a2bcde6c640a237224a3ae4b005064acde5fd160580a3b06"
skills: []
risk: medium
---
# Spec: Idempotency keys for refunds

## Summary
`POST /v1/refunds` accepts an optional `Idempotency-Key` header. The first request with a key creates the refund and stores the key with a fingerprint of the request. A retry with the same key and the same body returns the stored response. A retry with the same key and a different body is rejected. Requests without a key behave as before.

## Requirements
- FR-001 Replay. WHEN a request carries a key already stored for the same merchant and the request body matches THE SYSTEM SHALL return the original status code and body without creating a refund.
- FR-002 First use. WHEN a request carries a key not yet stored for the merchant THE SYSTEM SHALL create the refund and store the key, the request fingerprint and the response in one database transaction.
- FR-003 Mismatch. IF a stored key is reused with a different request body THEN THE SYSTEM SHALL respond 422 and create no refund.
- FR-004 Concurrency. WHILE a request with a given key is still being processed THE SYSTEM SHALL make a concurrent request with the same key wait for it or respond 409, never create a second refund.
- FR-005 Expiry. WHEN a stored key is older than 24 hours THE SYSTEM SHALL treat it as unused.
- FR-006 No key. WHEN a request carries no `Idempotency-Key` THE SYSTEM SHALL behave exactly as it does today.

## Success criteria
- SC-001 100 concurrent identical requests with one key create exactly one refund.
- SC-002 Duplicate refunds reported to support fall below 5 a month within 60 days of release.
- SC-003 p95 latency of requests without a key changes by less than 5%.

## Design
Add table `refund_idempotency(merchant_id, key, request_hash, response_status, response_body, created_at)` with a unique index on `(merchant_id, key)`. The refunds handler inserts the row first; a unique-violation means a retry, so it reads the stored row and either replays (hash equal) or returns 422 (hash differs). A nightly job deletes rows older than 24 hours. The header is parsed in the handler, so no other endpoint changes.

## Policy conformance
| Policy / skill | Status | Notes |
|---|---|---|
| PCI scope | compliant | Keys are opaque; no card data is stored in the new table. |
| Data retention | compliant | Rows are deleted after 24 hours. |

## Flagged concerns
None

## Out of scope
Idempotency for other endpoints, and changing how refunds are settled.

## Open questions
None

## Clarifications
### Session 2026-10-15
- Q: Reject or replay when a key is reused with a different body? → A: Reject with 422 (Priya Nair)

## Change log
