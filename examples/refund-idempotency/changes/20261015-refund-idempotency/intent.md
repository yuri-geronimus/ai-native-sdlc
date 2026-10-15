---
id: 20261015-refund-idempotency
title: Idempotency keys for refunds
author: Priya Nair
status: draft
tier: standard
source: human
record_id: ""
created: "2026-10-15T15:46:00Z"
---
# Intent: Idempotency keys for refunds

## Problem
Merchants' integrations retry `POST /v1/refunds` when a request times out. Because the endpoint has no way to recognise a retry, a timeout followed by a retry can refund the same transaction twice. Support sees about 40 duplicate refunds a month, each needing a manual clawback from the merchant's next settlement.

## Proposed outcome
A merchant can send an `Idempotency-Key` header with a refund request. Sending the same key again returns the original result and never creates a second refund. Merchants who don't send the header see no change.

## Affected users and systems
Merchant integrators (API clients), the support team who handle clawbacks, the `refunds` module of the payments API and its database.

## Constraints
- Existing clients that send no key must keep working unchanged.
- No new personal data may be stored. Keys are opaque strings chosen by the client.
- Refund amounts and currency handling stay as they are.

## Assumptions
- A 24-hour retention window for keys covers realistic retry behaviour.
- Keys are scoped per merchant, so two merchants may use the same key.

## Open questions
- Should a reused key with a different request body be rejected, or return the original result? (Recommended: reject with 422, as card networks do for mismatched retries.)

## Change log
