# Stage 6 (Maintain): control bands, recurring scans, on-call, closing the loop

Use this file to close the loop. A trigger invokes Claude with no person in the invocation path, and what Claude finds re-enters the pipeline as an intent.md (the Stage 1 (Plan) artifact). Detection stays deterministic. Claude is invoked only after a band is breached, and the tier sets what it may do. It acts only through gated routes: a PR into the review gate, or a runbook approved in advance. The file covers three playbook plays: "Closing the loop", "Recurring codebase scans" and "Claude on call with Claude Tag". It also gives the procedure for the interactive `diagnose` mode. Stage 5 must exist first: the review gate, the approval-gate hooks and a rehearsed rollback (see references/stage-5-deploy.md).

## Contents
- What changes in Maintain
- Play: closing the loop (control bands)
  - bands.yaml
  - The deterministic wrapper and why the model holds no write credentials
  - Confidence gate between headless stages
  - Triage queue
  - The three playbook examples
- Play: recurring codebase scans
- Play: Claude on call with Claude Tag
- Procedure: `diagnose` mode
- Pitfalls

---

## What changes in Maintain

Traditional: maintenance is reactive. A 3 a.m. alert can be missed, a ticket sits in the backlog, and post-mortem actions may never reach the codebase. AI-native: a trigger invokes Claude without a person in the path. The trigger can be a control-band breach, a ticket, a channel message or a schedule. Claude diagnoses, acts only through gated routes, and writes an intent.md that goes through Stages 1–5 like any other change. People triage and review that work. They no longer have to start it.

The end state is a headless chain, for example: bug ticket → intent.md → spec → plan → build → test → review. It has an **independent confidence gate between stages**: a deterministic check or an adversarial reviewing agent decides whether each stage's output continues or is escalated to a human. The confidence gate never replaces the human `intent`, `spec`, `plan`, `merge` and `release` gates. It decides only whether automated drafting continues or a human is pulled in now.

---

## Play: closing the loop (control bands)

**Prerequisites.**
- The intent.md format (Stage 1), which gives the loop a structured output to restart from.
- The Claude-accelerated PR review gate, approval-gate hooks as the action boundary, and a rehearsed CI/CD rollback path. The highest tier invokes that rollback.
- Infrastructure:
  - a metrics store the detector can query (Prometheus, the CI system's API, or equivalent);
  - read access to the repo;
  - a way to run Claude Code non-interactively in CI, or the Agent SDK for a service that receives webhooks.

### Steps

```
- [ ] 1. Service owner / platform engineer picks ONE metric with a stable rolling baseline
- [ ] 2. Write bands.yaml (template ../assets/templates/bands.yaml); commit it; run bands.py self-test
- [ ] 3. Add a golden-series test for this config: bands.py check on a fixture series in CI
- [ ] 4. Wire the trigger: ../assets/github/control-bands.yml (every 30 min), a monitoring webhook, or an in-network cron
- [ ] 5. Replace the workflow's default metrics-fetch step (CI failure rate) with a deterministic query that writes the series file
- [ ] 6. Run bands.py respond --dry-run against real history; check the tier you get for past incidents
- [ ] 7. Enable routes only after the dry runs look right (workflow: SDLC_APP_ID, SDLC_EXECUTE_RUNBOOKS; CLI: --execute-routes)
- [ ] 8. Owner triages the queue daily: fix now / schedule / dismiss; dismissals tune bands.yaml via PR
- [ ] 9. When a fix ships, add evals/incident-<ID>.json (references/stage-4-test.md)
```

1. **One metric.** Good first choices are CI test failure rate, post-deploy 5xx rate, or PR cycle time. Pick a metric whose baseline is stable over a rolling window.
2. **Detection is deterministic and tested.** [bands.py](../scripts/bands.py) computes mean and σ over a rolling baseline and applies Western Electric or Nelson rules, so it catches slow drift as well as spikes. No model is involved. `python3 <skill-dir>/scripts/bands.py self-test` runs golden series for every rule. The playbook requires the script to be "version controlled and unit tested", so keep a fixture per config too.
3. **Trigger layer.** This can be a scheduled GitHub/GitLab workflow, a webhook from the monitoring stack, or a cron job inside the network. Claude runs stateless: as a non-interactive CI step, or as an Agent SDK service in a sandboxed container. Because nothing is interactive, a loop can begin and end without anyone starting it.
4. **Tiers** come from version-controlled config:
   - **1σ**: log only;
   - **2σ**: invoke Claude read-only to diagnose;
   - **3σ**: Claude may act, but only by opening a PR into the review gate or triggering a pre-approved runbook.

### bands.yaml

This is the playbook example, extended. Every key except `metric`, `baseline`, `rules` and `tiers` is optional.

```yaml
metric: ci_test_failure_rate
baseline: rolling_30d          # or rolling_<N> = last N points before the eval window
rules: western_electric        # or nelson, or a list: [WE1, WE2, WE4]
rule_tiers: { WE4: 2sigma }    # optional: make slow drift diagnose instead of log
sigma: moving_range            # MR-bar/1.128 (robust for low-n ops metrics); or sample
direction: up                  # failure rates: only a rise should act
min_points: 20                 # fewer baseline points → tier none (with reason)
eval_window: 8                 # newest points scanned by rules, excluded from the baseline
sigma_floor: 0.005             # avoid divide-by-zero on a flat baseline
diagnose_max_budget_usd: 1.0   # --max-budget-usd per diagnosis run (default 1.0)
exclusions:                    # known deploy windows, holidays, incidents already handled
  - { from: "2026-12-24T00:00:00Z", to: "2027-01-02T00:00:00Z" }
tiers:
  1sigma: { action: log }
  2sigma: { action: diagnose, tools: "Read,Grep,Bash(gh run view *)" }
  3sigma: { action: propose, routes: [pull_request, "runbook:rollback-deploy"] }
```

Why these choices:
- **The eval window is excluded from the baseline.** Otherwise a drift drags the mean along with it, and the run rules (WE4, N2) never fire.
- **Alerts fire only when the newest point participates.** The alert then fires once, at onset, rather than every run.
- **Highest tier wins.** When several rules hit, the response follows the most severe one.
- **Quote `"runbook:rollback-deploy"`.** It contains a colon, and quoting avoids ambiguity in the stdlib YAML subset.
- **Rate metrics need enough volume.** For a rate with a small denominator (failures out of 3 runs), σ is meaningless. Aggregate to windows with enough volume before writing the series, or drop low-volume points. `bands.py` does not implement a p-chart.

Run the detector:

```bash
python3 <skill-dir>/scripts/bands.py check --config bands.yaml --series series.json --exit-code   # 0 none, 10 log, 20 diagnose, 30 propose
python3 <skill-dir>/scripts/bands.py respond --config bands.yaml --series series.json --repo . --dry-run
```

### The deterministic wrapper and why the model holds no write credentials

`bands.py respond` runs `check` and then acts per tier:

| Tier action | What runs | Model's tools |
|---|---|---|
| `log` | Appends a JSON line to `.claude/sdlc-state/bands-log.jsonl` (or `--log FILE`) | none; no model |
| `diagnose` | `SDLC_NONINTERACTIVE=1 claude -p <diagnosis prompt + check JSON> --permission-mode dontAsk --tools "Read,Grep,Glob,Bash" --allowedTools <Read,Grep,Glob + tier tools> --output-format json --json-schema <intent.schema.json> --max-turns 25 --max-budget-usd <diagnose_max_budget_usd> --settings '{"permissions":{"blockReadsOutsideWorkingDirectories":true}}' --strict-mcp-config`, then `sdlc.py new <slug> --source incident --from-json <structured_output> --no-activate --json` | read-only, inside the repo; Bash only as allowed by tier `tools` |
| `propose` | Same as diagnose, then each route: `pull_request` → git/gh commands that commit the intent on a branch and open a PR; `runbook:<name>` → runs `runbooks[<name>]` from `.claude/sdlc.json`, or reports "runbook not pre-approved" | same read-only set |

From the CLI, routes execute only with `--execute-routes`, and `--dry-run` prints every command instead of running it. The shipped [control-bands.yml](../assets/github/control-bands.yml) does not pass `--execute-routes`: its model-free propose and runbook jobs run the routes, and the runbook job checks the deploy window itself (`SDLC_RUNBOOK_REQUIRE_DEPLOY_WITHIN_MIN`, default 60 minutes, against `SDLC_DEPLOY_ENVIRONMENT`). `--execute-routes` is the path for non-GitHub wrappers.

Why it is built this way:
- **`tools:` in bands.yaml is a grant, not a restriction.** It maps to `--allowedTools`, which pre-approves calls and limits nothing on its own. The restriction comes from `--tools "Read,Grep,Glob,Bash"`, which removes every other built-in tool, together with `--permission-mode dontAsk`, which denies anything not allowed. So `Bash(gh run view *)` works and `Bash(git push *)` is denied. `bands.py` refuses the obvious unsafe grants outright: bare `Bash`, `Bash(*)`, `Edit`/`Write`, commands that run arbitrary code (`Bash(sh *)`, `Bash(python3 *)`, `Bash(env *)` …) and write commands (`git push/commit/reset`, `gh pr merge`, `gh release`, `gh workflow run`, or a wildcard such as `Bash(git *)` that covers them). Reviewing bands.yaml is the control for the rest. dontAsk still runs Claude Code's built-in read-only Bash commands (for example `ls`, `cat`, `grep`, `date`, read-only `git`) with no grant, but only on paths inside the working directories (cwd and `--add-dir`); the same commands on paths outside them are denied.
- **The model never holds write or deploy credentials, even at 3σ.** It returns a structured diagnosis. The wrapper is deterministic code reviewed in git, and it writes the intent, opens the PR and runs the runbook. The playbook's lesson from an incident agent applies here: draw boundaries around access and actions, not around model instructions.
- **Not `--bare`.** The diagnosis benefits from CLAUDE.md and project skills, and the repo's hooks, including the production gate, must run.
- **The runbook belongs to the wrapper.** A runbook is a fixed, pre-approved command in version control. Only the wrapper runs it, never the model.
- **Revert and quarantine PRs come from elsewhere.** For example, "quarantine the flaky test" or "revert commit X". The diagnosing model cannot write, so the intent names the change, and a separate write step that can only open PRs produces it: a human, or claude-code-action with `acceptEdits` on a branch. The review gate decides. `bands.py` itself ships only the intent PR route. The shipped [claude-mention.yml](../assets/github/claude-mention.yml) has one narrow exception for this: on a change with no plan.md whose intent has `source: incident` and names a revert or test quarantine, `@claude implement only the quarantine/revert named in changes/<id>/intent.md` implements exactly that (a quarantine skips or tags the test, never changes assertions), and the PR's reviewers decide. Under `GATE_CHECK=enforce` the sdlc-artifacts check stays red on that PR because it touches source without an approved plan; a human merges it deliberately.
- **No production credentials in the workflow.** `control-bands.yml` sets permissions per job. Only the model-free propose job holds `contents: write`, `pull-requests: write` and `issues: write` (for labels); the optional runbook job holds `actions: write` and `deployments: read`, runs in the `sdlc-runbooks` environment and only when `SDLC_EXECUTE_RUNBOOKS=true`. The job where the model runs is read-only. The workflow does nothing until `SDLC_BANDS_ENABLED=true`.

### Confidence gate between headless stages

After `respond` creates an intent, and between any further headless stages, run an independent gate before anything continues:

```bash
python3 <skill-dir>/scripts/sdlc.py validate <id> intent              # deterministic: structure, placeholders
SDLC_NONINTERACTIVE=1 claude -p "artifact: changes/<id>/intent.md; upstream: <dir>/check.json (the band-breach evidence). Return the verdict." \
  --append-system-prompt-file .claude/agents/sdlc-gate.md --permission-mode dontAsk --tools "Read,Grep,Glob" \
  --settings '{"permissions":{"blockReadsOutsideWorkingDirectories":true}}' --add-dir <dir> --strict-mcp-config \
  --max-turns 15 --max-budget-usd 1 --output-format json \
  --json-schema "$(cat <skill-dir>/assets/schemas/gate-verdict.schema.json)"
```

This matches the gate step in control-bands.yml. There is no `--allowedTools`: dontAsk already allows reads in the checkout and `--add-dir`, and a bare `Read` grant would approve reads of any path. The shipped schemas declare no `$schema`; the CLI rejects a draft 2020-12 `$schema` URI with exit 1 and empty stdout, so strip it from any schema you add.

[sdlc-gate.md](../assets/agents/sdlc-gate.md) returns `{"verdict": "continue"|"escalate", "confidence", "reasons", "blocking_issues"}`.
- **`escalate`** goes to a human now: page or label the PR `needs-human`. The agent defaults to escalate when uncertain.
- **`continue`** lets the intent enter the triage queue, or lets the next automated drafting step run.

Why not `--agent sdlc-gate` (observed on v2.1.294):
- **`--agent` works with `-p`, but the agent's `tools: Read, Grep, Glob` hides the StructuredOutput tool.** `structured_output` comes back null while the result still reports `subtype: success`, so a wrapper reading `.structured_output` gets nothing. The CLI `--tools` flag does not have this effect. Appending the agent body as a system prompt keeps the tool; so does adding `StructuredOutput` to the agent's `tools:` line.
- **Frontmatter `permissionMode` is not applied to the main thread.** Always pass `--permission-mode dontAsk`.
- **Hooks in subagent frontmatter** are unverified in `-p` on an untrusted CI checkout. Keep must-hold hooks in `.claude/settings.json`.

Check the result object: treat a missing `structured_output` as `escalate`.

If the workflow you installed does not run the gate, add these two steps after `respond`.

### Triage queue

The queue has two sources:
- **Open `sdlc-triage` issues.** These are the 2σ findings, plus 3σ findings when the propose job has no App token. control-bands.yml puts the diagnosis intent in the issue body and the change folder in the run's `bands-diagnose` artifact; the 2σ tier never writes to the repo.
- **Open changes** whose intent.md has `source: incident` (or `scan`, `ticket`): 3σ intent PRs, `diagnose` mode and scan findings.

```bash
gh issue list --label sdlc-triage --state open --json number,title,createdAt,labels
grep -l '^source: \(incident\|scan\|ticket\)' changes/*/intent.md
python3 <skill-dir>/scripts/sdlc.py status          # open changes only
```

The service owner or on-call engineer decides each item. Route product-facing findings to the product owner. An agent-written intent always needs the intent gate, even for a bounded change.

| Decision | Change in `changes/` | `sdlc-triage` issue |
|---|---|---|
| Fix now | `sdlc.py activate <id>`, then `sdlc.py present <id> intent`, then the normal gates (Stages 1–5) | A human commits `intent.md` and `audit.md` from the artifact's `change/<id>/` folder as `changes/<id>/` on branch `change/<id>` (the hooks deny agent writes to audit.md); then as for a change |
| Schedule | Leave as draft; record the tracker id: `sdlc.py log <id> NOTE --message "scheduled: <ticket>"` | Keep the issue open; comment the tracker id |
| Dismiss | `sdlc.py log <id> NOTE --message "dismissed: <reason>"`, then `sdlc.py close <id> --status dropped`, then a PR tuning bands.yaml | `gh issue close <n> --comment "dismissed: <reason>"`, then a PR tuning bands.yaml |

Dismissals tune the bands and reduce noise. To tune, add an `exclusions` range, set `direction`, raise `sigma_floor` or `min_points`, or change `rule_tiers`. Make the tuning a reviewed commit, so the history shows why a band moved.

### The three playbook examples

| Example | Config | Route |
|---|---|---|
| CI test failure rate breaches 3σ → quarantine the flaky test or open a revert PR; the review gate decides | the bands.yaml above | `pull_request`: the intent PR, then the quarantine/revert from a human or a PR-only write step (see "Revert and quarantine PRs" above) |
| Post-deploy 5xx rate breaches 3σ **with a deployment in the window** → trigger the existing rollback pipeline | `metric: http_5xx_rate`, `direction: up`, `3sigma: { action: propose, routes: ["runbook:rollback-deploy"] }` | `runbook:rollback-deploy`. "Deployment in the window" is not a bands.yaml key: the shipped runbook job checks it deterministically (`SDLC_RUNBOOK_REQUIRE_DEPLOY_WITHIN_MIN`, `SDLC_DEPLOY_ENVIRONMENT`) and runs only with `SDLC_EXECUTE_RUNBOOKS=true`. A non-GitHub wrapper checks it itself (e.g. `gh api "repos/O/R/deployments?environment=production" --jq '.[0].created_at'`) and passes `--execute-routes` only when true |
| PR cycle time trips a drift rule → report for engineering leadership | `metric: pr_cycle_time_h`, `rule_tiers: { WE4: 2sigma }`, `2sigma: { action: diagnose }` | the diagnosis intent is the report; route it to engineering leadership in triage. The harness works for process metrics too |

### Governance

| | |
|---|---|
| Enforced | Tier boundaries from version-controlled bands.yaml; `--tools` + `dontAsk` for the model; permissions and managed settings deny production access; runbooks only from `.claude/sdlc.json`. |
| Evidence | Check JSON (baseline, z, rule hits, tier); the diagnosis intent with tagged evidence; the gate verdict. |
| Logged | `bands-log.jsonl`, workflow run logs and artifacts, `changes/<id>/audit.md` (CHANGE_CREATED source=incident, NOTE, CHANGE_CLOSED), all timestamped. |
| Approves | The service owner triages and approves findings. Resulting changes go through the normal PR review gate. Runbooks were approved in advance. |

### Measure

- **Leading: time from band breach to an intent.md in the triage queue**, against the old time from incident to post-mortem action. The breach is `latest.ts` in the check JSON (also the `ts=` in each issue's and PR's `sdlc-band` marker). Keep `respond` output as a workflow artifact. Intent arrival is the `created` frontmatter for changes, and the issue's `createdAt` for 2σ findings.
  ```bash
  for f in $(grep -l '^source: incident' changes/*/intent.md); do grep -m1 '^created:' "$f" | sed "s|^|$f |"; done
  gh issue list --label sdlc-triage --state all --limit 500 --json number,createdAt --jq '.[] | "#\(.number) \(.createdAt)"'
  ```
- **Lagging: share of findings that become merged fixes.** This compares the triage queue with the PR history. Count both sources, so dismissed 2σ issues count in the denominator:
  ```bash
  for f in $(grep -l '^source: incident' changes/*/intent.md); do
    grep -h CHANGE_CLOSED "$(dirname "$f")/audit.md" | grep -o 'status=[a-z]*' || echo status=open; done | sort | uniq -c
  gh issue list --label sdlc-triage --state all --limit 500 --json state --jq 'group_by(.state) | map({(.[0].state): length}) | add'
  ```
- **Lagging: repeat incidents of the same class.** Take these from the tracker. They should fall as fixes add eval cases.

---

## Play: recurring codebase scans

**What changes.** A scan is a point-in-time statement about a codebase under a particular model, and both halves go stale. Traditional: a scan is an event before a release or an audit, and the backlog is worked by hand. AI-native: scans run on a schedule with no human in the invocation path, on the most capable model available, with findings validated before anyone reads them. Each finding is handled like a breached band: a fix that fits in one PR goes through the review gate, and anything larger becomes an intent.md. Coverage is dated from the last run, not the first.

**Prerequisites.** The PR review gate and approval-gate hooks (Stage 5); the intent.md format (Stage 1) for findings too large for one PR. Infrastructure for **Claude Security**, the hosted scanner (public beta, Claude Enterprise):
- the Anthropic GitHub App installed on the target repositories (cloud-hosted github.com);
- Claude Code on the web enabled;
- Extra Usage turned on with a spend limit;
- premium seats for the people who run scans;
- the feature switched on by an admin at `claude.ai/admin-settings/claude-code`.

Scans are billed on consumption, so size the spend limit to the number and size of repositories. The playbook names the scan model as "Claude Mythos 5". That is unverified here, so do not hard-code model ids.

### Steps

```
- [ ] 1. Security lead connects repos and groups them into projects by repo, service or team (ownership clear from day one)
- [ ] 2. First full scan of the most critical repos, including ones scanned before: this is the baseline
- [ ] 3. Schedule per project: weekly for active services; scope to a directory/branch for large or mixed repos
- [ ] 4. Triage with the confidence rating; dismiss with a reason so it does not return as new
- [ ] 5. Bounded finding: open the suggested patch in Claude Code on the web, review, send through the PR gate
- [ ] 6. Wider finding (architectural weakness, pattern across services): sdlc.py new <slug> --source scan → Stage 1
- [ ] 7. After the fix is released: add an eval for the vulnerability class (evals/incident-<ID>.json or scan-<ID>.json)
- [ ] 8. Export findings (CSV / Markdown / webhooks) to the existing tracker and audit system of record
```

- **The baseline will surface findings in code considered clean.** Expect this and budget triage time for the first run.
- **The agent that proposed a patch has no route to approve it.** The patch PR needs a code-owner approval like any other change. Put `Finding: <id>` in the PR body so time-to-PR can be measured.
- **Wider findings start at Plan.**
  ```bash
  python3 <skill-dir>/scripts/sdlc.py new authz-pattern-orders --source scan --tier high-risk --record-id SEC-412
  ```
- **The scanner augments SAST and SCA, it does not replace them.** Deterministic static analysis and dependency scanning stay in CI. The model-driven scan covers the context-dependent vulnerabilities those tools are not built to find.

### Governance

| | |
|---|---|
| Enforced | Central admin controls: connected repos, who holds a scan seat, the spend limit. Fixes reach production only through the PR gate and branch protection. |
| Evidence | Each finding's validation result and confidence rating; each dismissal's reason. |
| Logged | The scan history (found, fixed, consciously accepted); exports in the tracker; PR history for patches. |
| Approves | The security lead triages; code owners approve patches; wider changes take the normal Stage 1–3 gates. |

### Measure

- **Leading: share of connected repos on a schedule**, from the Claude Security project configuration or export. **Time from finding reported to patch in the PR gate**: take the export timestamp and compare it with the creation time of the PR that names the finding:
  ```bash
  gh pr list --state all --search "Finding: in:body" --limit 200 --json number,createdAt,body | jq '.[] | {n:.number, at:.createdAt, finding:(.body|capture("Finding: (?<id>\\S+)").id)}'
  ```
- **Lagging: vulnerabilities found by scheduled scans vs found in production or by external report**, from the incident tracker. Also track the trend in findings per scan on repos with several runs, from successive exports. It should fall as fixes and evals accumulate.

---

## Play: Claude on call with Claude Tag

**What changes.** Incidents also arrive as messages, for example a 10 p.m. Slack message in an incident channel. With **Claude Tag**, Claude is a member of those channels under its own identity, so every incident gets a first responder immediately. The response becomes part of the loop and part of the memory for future incidents. Anyone in the channel can guide it, test hypotheses and investigate in real time. The channel is the audit trail: request, diagnosis, human authorization and fix all stay where the incident was handled.

**Facts to set it up** (public beta; Team/Enterprise; first-party service; Slack):
- Admins enable it at `claude.ai/admin-settings/claude-tag`. Users pair with `@Claude connect` in Slack.
- In channel sessions Claude acts under the org's shared identity, not a developer's. Work runs in an ephemeral sandbox, billed from a usage balance with a spend limit.
- Connect observability (Grafana, Datadog, logs, PagerDuty, GitHub, k8s) through MCP connectors. Connectors, plugins and skills can be set per org, workspace or channel.
- Keep standing instructions as files in a GitHub repo:
  - `oncall.md`: routing, policies, paging criteria. For example: "page if error rate > 2% for > 5 min outside a known deploy window; else log to lessons.md".
  - `lessons.md`: an append-only lessons log.

  This file uses `oncall/oncall.md` and `oncall/lessons.md`; adjust the paths to your repo.

### Steps

```
- [ ] 1. Add Claude to the on-call channel; connect metrics/logs/tickets via MCP, read-only
- [ ] 2. Commit oncall.md and lessons.md; make the paging rule deterministic (alerting stays deterministic)
- [ ] 3. Standing instruction: read lessons.md before every investigation; append the post-mortem after
- [ ] 4. Investigate: query the data first, then theorize; post evidence-grounded findings in the thread
- [ ] 5. Small, well-bounded fix → PR through the review gate (named human owner, approval to merge, same CI gates)
- [ ] 6. Anything larger → intent.md for Stage 1 (sdlc.py new <slug> --source incident)
- [ ] 7. After mitigation: verify via MCP that the metric is back at baseline; confirm in the thread
- [ ] 8. Promote recurring lessons into skills; turn shipped fixes into evals
```

- **The on-call agent is read-only.** It can read logs, post in channels and write docs. It cannot deploy. Rollouts and canary ramps run through the release pipeline, or through a separate agent working under an engineer's permissions with the normal gates.
- **"Query the data first, then theorize."** A hypothesis posted before evidence anchors everyone in the channel.
- **Protect lessons.md as append-only in CI.** Add a required check that fails if a PR deletes lines from it:
  ```bash
  git diff --numstat origin/main...HEAD -- oncall/lessons.md | awk '$2 > 0 { exit 1 }'
  ```
- **Tickets work the same way.** Tagged on a ticket over MCP or asked in the channel, Claude triages: a small fix becomes a PR, and a larger one becomes an intent.md. From there the loop feeds itself.

### Governance

| | |
|---|---|
| Enforced | Read-only tool surface for the on-call agent; every PR has a named human owner, needs approval to merge, and goes through the same CI gates; paging is deterministic. |
| Evidence | Queries and their results posted in the thread; metric back at baseline. |
| Logged | The channel history; lessons.md in git; PRs and intents. |
| Approves | The on-call engineer authorizes actions in the thread; code owners approve PRs; release manager for production. |

### Measure

- **Leading: time from incident open to the first evidence-grounded analysis in the thread**, from channel timestamps. Anthropic reports a 14-minute median.
- **Lagging: repeat incidents of a class already in lessons.md**, from the tracker. Also the share of lessons promoted to skills or evals: compare the lesson count with the skills and evals added in the same window.
  ```bash
  grep -c '^## ' oncall/lessons.md
  git log --diff-filter=A --since=90.days --name-only --format= -- .claude/skills evals | sort -u | wc -l
  ```

---

## Procedure: `diagnose` mode

`diagnose [<alert|metric|ticket|incident-id>]` is the interactive form of the 2σ tier. Stay read-only throughout. Do not edit code, deploy, restart or run runbooks: mitigation goes to the on-call engineer or the wrapper.

```
- [ ] 1. Pin the signal: metric/alert, time window, first-seen time, current value
- [ ] 2. If a series and bands.yaml exist: bands.py check → tier, z, rule hits
- [ ] 3. Read lessons.md (if present) and prior incident intents for the same system
- [ ] 4. Query data first: logs, gh run view --log-failed <id>, deployments and merges in the window, dashboards via MCP
- [ ] 5. List 1-3 hypotheses, each with evidence for/against; tag every claim [source:<record-id>] or [assumption]; keep the query behind it
- [ ] 6. Ask the human at most 5 questions, one at a time, each with your recommended answer
- [ ] 7. Write the intent: sdlc.py new <slug> --bug --source incident [--record-id INC-…] --no-activate (diagnosis stays read-only and must not replace the session's active change); fill Current/Expected/Unchanged/Evidence
- [ ] 8. sdlc.py validate <id>; run the sdlc-gate subagent → continue / escalate
- [ ] 9. Recommend triage: fix now / schedule / dismiss, and the owner; name any pre-approved runbook for the human to run
- [ ] 10. Append the lesson (what was seen, root cause or best hypothesis, what disambiguated it)
```

Commands:

```bash
python3 <skill-dir>/scripts/bands.py check --config bands.yaml --series series.json
git log --since="<window start>" --merges --format='%h %cI %s' origin/main
gh api "repos/OWNER/REPO/deployments?environment=production&per_page=5" --jq '.[]|{sha,created_at}'
python3 <skill-dir>/scripts/sdlc.py new 5xx-after-cache-deploy --bug --source incident --record-id INC-2291 --no-activate
python3 <skill-dir>/scripts/sdlc.py validate <id>
```

Use the bug template for regressions and defects (Current / Expected / Unchanged behavior / Evidence). The Unchanged clauses become regression tests at fix time. Use the plain intent template for drift and process findings, where there is no "expected" to restore. Put the queries and their outputs in `## Evidence`. A reader must be able to rerun them. Use the claim tags from references/stage-1-plan.md: `[source:<record-id>]` for what the incident, alert or query shows, and `[assumption]` for anything without data behind it. An assumption that would change the fix also goes in `## Open questions`.

---

## Pitfalls

- **A model in the detector.** Detection must be deterministic and unit tested. The model is invoked only after a breach.
- **Treating `tools:` as a sandbox.** It is a grant. Without `--tools` and `dontAsk`, the diagnosing model inherits everything.
- **Write or deploy credentials in the loop's job.** Even a well-instructed model can be prompt-injected through logs it reads.
- **Baseline including the evaluation window.** Drift then hides itself.
- **Low-volume rates.** At low volume σ is noise, and the bands page constantly.
- **Dismissing without tuning.** The same false alarm returns every run, and the queue gets ignored.
- **Unrehearsed runbook.** A 3σ rollback that has never run in staging is a second incident.
- **Theorizing before querying.** It anchors the investigation. Post evidence first.
- **No incident eval.** The same class recurs. Close every shipped fix with a permanent eval.
