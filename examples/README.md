# Examples

| Example | Tier | Shows |
|---|---|---|
| [refund-idempotency](refund-idempotency/) | standard | A complete change folder for a payments API: intent.md, spec.md with EARS requirements, plan.md with proof commands and rollback, and the start of the audit log. |

## Reading the refund-idempotency example

Start in [changes/20261015-refund-idempotency/](refund-idempotency/changes/20261015-refund-idempotency/) and read the files in this order:

1. **intent.md** is the product owner's request in plain language: problem, outcome, constraints and open questions. No design.
2. **spec.md** turns it into numbered requirements (FR-001…) and success criteria (SC-001…), with policy conformance and flagged concerns.
3. **plan.md** says which files change, the order of work, how it will be proved and how to roll it back. Every FR and SC id from the spec appears in it, which `sdlc.py trace` checks.
4. **audit.md** is written by `sdlc.py`, never by hand. Here it stops after the intent gate was presented, because approvals come from a human in an interactive session and are not part of a sample.

The artifacts pass the skill's own checks. `sdlc.py` works on the git root, so copy the example into a scratch repo first:

```sh
d=$(mktemp -d) && git init -q "$d" && cp -R examples/refund-idempotency/changes "$d"/
python3 scripts/sdlc.py --repo "$d" validate 20261015-refund-idempotency --strict
python3 scripts/sdlc.py --repo "$d" trace 20261015-refund-idempotency
```

The payments service the plan refers to is fictional; there is no code to build.
