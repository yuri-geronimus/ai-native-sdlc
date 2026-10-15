# Metrics: is each play working?

Use this file when someone asks whether the AI-native SDLC is paying off, wants the leading or lagging indicator for a play, or needs the exact command that computes it. Every play in the playbook names one leading indicator (moves within days, tells you the play is being used) and one lagging indicator (moves over weeks, tells you it is producing the outcome). This file gives each one with its data source and a concrete command or query. Measure to learn, not to rank: DORA's own guidance is to measure per service and not turn the metrics into targets, because a targeted metric gets gamed (intents rubber-stamped to raise survival, trivial PRs split to raise throughput).

## Contents
- Reporting rules
- One row per play
- The built-in report: `sdlc.py metrics`
- Recipes: git
- Recipes: GitHub (`gh`)
- Recipes: hook and band logs
- Recipes: OpenTelemetry
- DORA definitions and how to compute them
- Pitfalls

---

## Reporting rules

- Report **median and p90**, never the mean. Lead times are long-tailed; one stuck change moves the mean and hides the typical case.
- Take a **baseline before** adopting a play (four weeks of the old process from git and PR history), then compare like with like per service.
- **Pair** every speed metric with a quality metric from the same play (intent lead time with survival rate, merges per engineer with rework rate). Report them together.
- Segment by tier. Trivial and high-risk changes should not share a median.
- Write down the definition next to the number (which timestamp, which filter). Most metric disputes are definition disputes.

Median and p90 of a column of numbers (nearest-rank p90):

```bash
stats() { python3 -c 'import sys,math,statistics as s
x=sorted(float(v) for v in sys.stdin.read().split())
print("n=%d median=%.2f p90=%.2f" % (len(x), s.median(x), x[math.ceil(0.9*len(x))-1]) if x else "n=0")'; }
```

## One row per play

| Play (stage) | Leading indicator | Lagging indicator | Data source and command |
|---|---|---|---|
| Intent capture (1 Plan) | Time from first conversation (`CHANGE_CREATED`) to committed intent.md; expect weeks → hours | Survival rate: intents approved vs rejected or dropped (`close --status dropped` counts as rejected); intent commits after the first spec commit | `sdlc.py metrics`; git first-add dates; `audit.md` `GATE_APPROVED`/`GATE_REJECTED gate=intent`, `CHANGE_CLOSED status=dropped` |
| Requirements and design (2 Design) | intent.md commit → spec.md commit for the same change | Requirements rework: spec.md commits after the first plan.md commit | `sdlc.py metrics`; git recipe "commits after" below |
| Plan mode (3 Build) | Share of PRs merged from the first implementation pass; plan approval → merged PR | Rework cycles per change (pushes after first review); share of merged diffs still matching plan.md | `gh pr view --json commits,reviews`; `sdlc.py drift <id>` (zero unplanned files = match) |
| CLAUDE.md (3 Build) | How often Claude repeats a mistake CLAUDE.md should have caught; corrections committed per month | Time to first merged PR for a new team member | `git log -- CLAUDE.md`; PR label `repeat-mistake`; `gh pr list --author` |
| Skills as institutional knowledge (3 Build) | Policy owner's approval of a policy change → updated skill merged | PR review findings citing the policy, trending to zero (if not: the skill doesn't trigger or has drifted) | PR body `Policy-Approved:` date vs `mergedAt`; review comments matching the skill name |
| Hooks as build-time guardrails (3 Build) | Blocks per hook per week, and the share that were correct blocks | Protected-path or credential incidents reaching main | `hook-log.jsonl`; incident tracker |
| Parallel sessions and subagents (3 Build) | Concurrent sessions per engineer while review quality holds; share of the day steering vs waiting | Changes merged per engineer per week, read with the rework rate | OTel `claude_code.session.count`, `session.id`, `claude_code.active_time.total`; `gh pr list` |
| Feedback loop (4 Test) | First-pass CI success rate for agent-written changes | Review time per PR; change failure rate | `gh run list`; `gh pr list --json reviews`; Stop-hook blocks in `hook-log.jsonl` |
| Continuous evals in CI (4 Test) | Eval pass rate over time; incident → permanent eval case | Regressions caught in CI vs found in production | `run_evals.py --out results.json` (`pass_rate`); `git log --diff-filter=A -- evals/`; incident tracker |
| AI in the PR review loop (5 Deploy) | Time to first review (target: minutes); share of review comments resolved without a human touching the branch | Defects and vulnerabilities caught before merge vs escaped to production | `gh pr list --json createdAt,reviews`; commit authors after first review; Important finding counts vs incidents |
| Hooks as approval gates (5 Deploy) | Time waiting on each approval gate | Gate violations reaching production, before vs after hooks | `audit.md` `GATE_PRESENTED`→`GATE_APPROVED`; deploy job wait; OTel `tool_decision source=hook`; incident tracker |
| CI/CD integration (5 Deploy) | Share of pipeline failures triaged without paging a human | DORA: lead time, deployment frequency, recovery time, change fail rate, rework rate | `gh run list --status failure` vs paging tool; deployments API; trailers |
| Closing the loop (6 Maintain) | Band breach → finding in the triage queue (vs old incident → post-mortem action) | Share of findings that become merged fixes; repeat incidents of the same class | `bands.py` check JSON `latest.ts` / `bands-log.jsonl` vs `CHANGE_CREATED` (3σ intents) or the `sdlc-triage` issue's `createdAt` (2σ findings); `source: incident` intents with `status: done` |
| Recurring codebase scans (6 Maintain) | Share of connected repos on a schedule; finding reported → patch in the PR review gate | Vulnerabilities found by the scan vs in production or by external report; findings per scan trending down on re-scanned repos | Claude Security scan history/export; PR body `Finding:`; incident tracker |
| Claude on call (Claude Tag) | Incident opened → first evidence-grounded analysis in the thread (Anthropic reports a 14-minute median) | Share of incidents whose post-mortem lands in the lessons file; repeat incidents | Channel timestamps; `git log -- lessons.md` |

## The built-in report: `sdlc.py metrics`

```bash
python3 <skill-dir>/scripts/sdlc.py metrics --since <YYYY-MM-DD>          # markdown table
python3 <skill-dir>/scripts/sdlc.py metrics --since <YYYY-MM-DD> --json   # for a dashboard
```

It reads git history and every `changes/<id>/audit.md`, and reports per change the intent → spec → plan → PR/close durations (first-add commit author dates, approvals from the audit log), spec commits after the first plan commit (requirements rework), intent commits after the first spec commit, and survival, with medians and p90 in the summary. `--since` is the window start (UTC date); changes created before it are left out.

Survival counts only changes whose tier carries an intent gate: standard, high-risk, and bounded changes with an agent-written intent (source ticket, incident, scan or review). An intent approval counts as survived; a rejection or `close --status dropped` counts as not; open drafts are undecided and left out. Bounded changes with a human-written intent have no intent gate and are reported as excluded. When intent, spec and plan share one commit (a squash merge), the report warns and shows — for that change's stage durations and rework counts. Everything else in this file needs `gh`, the hook logs or the OTel backend.

## Recipes: git

Stage timestamps use the first commit that added the file, by author date (author dates survive rebases; committer dates don't):

```bash
first_add() { git log --diff-filter=A --format=%aI -- "$1" | tail -1; }
first_add changes/$ID/intent.md; first_add changes/$ID/spec.md; first_add changes/$ID/plan.md
```

Commits to a file after another file's first commit (requirements rework; intent changes after spec):

```bash
P=$(git log --diff-filter=A --format=%H -- changes/$ID/plan.md | tail -1)
git rev-list --count "$P"..HEAD -- changes/$ID/spec.md      # spec rework after plan
S=$(git log --diff-filter=A --format=%H -- changes/$ID/spec.md | tail -1)
git rev-list --count "$S"..HEAD -- changes/$ID/intent.md    # intent churn after spec
```

CLAUDE.md corrections per month, and skill-change history:

```bash
git log --since=180.days --format=%aI -- CLAUDE.md | cut -c1-7 | sort | uniq -c
git log --format='%h %aI %an %s' -- .claude/skills/secure-api-review/
```

Incident → eval latency (name eval cases after the change id of the incident):

```bash
git log --diff-filter=A --format=%aI -- "evals/$INCIDENT_ID*.json" | tail -1
```

Incident-sourced findings that became merged fixes:

```bash
total=$(grep -l '^source: incident' changes/*/intent.md | wc -l)
done=$(grep -l '^source: incident' changes/*/intent.md | xargs grep -l '^status: done' | wc -l)
echo "$done / $total"
```

Squash merges collapse branch history on main. Keep intent, spec and plan as separate merges (they are separate gates), or measure on the PR branch before merge.

## Recipes: GitHub (`gh`)

Time to first review, in hours:

```bash
gh pr list --state merged --limit 300 --json createdAt,reviews \
  --jq '.[] | select(.reviews|length>0) | [.createdAt, (.reviews|map(.submittedAt)|min)] | @tsv' |
python3 -c 'import sys,datetime as d
p=lambda s: d.datetime.fromisoformat(s.replace("Z","+00:00"))
for l in sys.stdin: a,b=l.split(); print((p(b)-p(a)).total_seconds()/3600)' | stats
```

Review time per PR (opened → merged): same query with `mergedAt` instead of the first review.

Rework cycles for one PR (pushes after the first review), and whether a human touched the branch:

```bash
gh pr view $N --json commits,reviews --jq '(.reviews|map(.submittedAt)|min) as $r
  | [.commits[] | select(.committedDate > $r)]
  | {pushes_after_review: length, human_commits: map(select(any(.authors[]; (.login // "")|test("claude")|not)))|length}'
```

First-pass CI success for agent-written changes (branches `change/*` or `claude/*`; first run per branch):

```bash
gh run list --workflow ci.yml --event pull_request --limit 1000 --json headBranch,conclusion,createdAt \
  --jq '[.[] | select(.headBranch|test("^(change|claude)/"))] | group_by(.headBranch)
        | map(sort_by(.createdAt)[0]) | {n: length, first_pass: (map(select(.conclusion=="success"))|length)}'
```

Changes merged per engineer since a window start (divide by the weeks in the window):

```bash
gh pr list --state merged --search "merged:>=<YYYY-MM-DD>" --limit 1000 --json author,mergedAt \
  --jq 'group_by(.author.login) | map({author: .[0].author.login, merged: length})'
```

Time to first merged PR for a new member: `gh pr list --author $LOGIN --state merged --search "sort:created-asc" --limit 1 --json mergedAt`, against the start date.

Review findings citing a policy, per month (self-hosted review comments; rename for your skill):

```bash
gh api --paginate repos/$O/$R/pulls/comments \
  --jq '.[] | select(.body|test("secure-api-review|API security standard")) | .created_at[0:7]' | sort | uniq -c
```

Repeated mistakes: label a PR `repeat-mistake` when review flags something CLAUDE.md already covers, then `gh pr list --state all --label repeat-mistake --json createdAt --jq '.[].createdAt[0:7]' | sort | uniq -c`.

Managed Code Review Important-finding count for a commit (the check run is always neutral; this is its tally):

```bash
gh api repos/$O/$R/check-runs/$CHECK_RUN_ID \
  --jq '.output.text | split("bughunter-severity: ")[1] | split(" -->")[0] | fromjson | .normal'
```

Production approval wait: the protected-environment deploy job is created when it starts waiting and started after approval, so `gh api repos/$O/$R/actions/runs/$RUN/jobs --jq '.jobs[] | select(.name|test("deploy-prod")) | [.created_at, .started_at] | @tsv'` approximates it.

## Recipes: hook and band logs

Hook decisions by hook and verdict (local, `.claude/sdlc-state/hook-log.jsonl`; collect from CI artifacts or a log shipper for team totals):

```bash
python3 -c 'import json,sys,collections as c
n=c.Counter()
for l in open(sys.argv[1]):
    try: e=json.loads(l)
    except ValueError: continue
    n[(e.get("event"),e.get("decision"))]+=1
for k,v in sorted(n.items(), key=str): print(v, *k)' .claude/sdlc-state/hook-log.jsonl
```

Gate wait from the audit log (hours from presentation to decision, per gate):

```bash
python3 -c 'import glob,re,datetime as d
p=lambda s: d.datetime.strptime(s,"%Y-%m-%dT%H:%M:%SZ")
for f in glob.glob("changes/*/audit.md"):
    seen={}
    for l in open(f):
        m=re.match(r"- (\S+) \| (GATE_PRESENTED|GATE_APPROVED|GATE_REJECTED) \| gate=(\w+)", l)
        if not m: continue
        ts,ev,g=m.groups()
        if ev=="GATE_PRESENTED": seen[g]=p(ts)
        elif g in seen: print(g, round((p(ts)-seen.pop(g)).total_seconds()/3600,2))'
```

Band breaches: `bands.py check … --exit-code` prints JSON whose `latest.ts` and `tier` are the breach time and tier; `log`-tier events accumulate in `.claude/sdlc-state/bands-log.jsonl`. The triage queue has two sources: open `sdlc-triage` issues (2σ findings, `gh issue list --label sdlc-triage --state open --json number,title,createdAt,labels`) and `changes/*/intent.md` (3σ and other sources). Count both. Dismissals (`gh issue close <n> --comment "dismissed: <reason>"` for issues, `close --status dropped` for changes) should feed `exclusions` or band tuning; track the dismissal share as the band's noise rate:

```bash
gh issue list --label sdlc-triage --state closed --limit 500 --json comments \
  --jq '[.[] | select(any(.comments[]; .body|startswith("dismissed:")))] | length'
```

## Recipes: OpenTelemetry

Enable the export through managed settings (see references/governance.md). Event and metric names as Claude Code emits them; your backend may rename them (Prometheus, for example, rewrites dots to underscores).

| Indicator | Signal |
|---|---|
| Concurrent sessions per engineer | `claude_code.session.count` by `user.email`; overlap of `session.id` first/last event timestamps |
| Steering vs waiting | `claude_code.active_time.total` (check which attributes your export carries before splitting user vs agent time) |
| Hook blocks | `claude_code.tool_decision` with `source=hook`, `decision=reject`; `claude_code.hook_execution_complete.num_blocking` |
| Approval-gate wait | gap between `claude_code.tool_decision` and `claude_code.tool_result` for the same `tool_use_id`; the `tool.blocked_on_user` span only with `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` (beta) |
| Bypass attempts | `claude_code.permission_mode_changed` (`from_mode`, `to_mode`) |
| Edit acceptance | `claude_code.code_edit_tool.decision` |
| Skill triggering | `claude_code.skill_activated` by skill name (a policy skill that never activates explains flat review findings) |
| Cost per change | `claude_code.cost.usage` by `session.id`, joined to the change via the branch |

## DORA definitions and how to compute them

| Measure | Definition (dora.dev) | Compute |
|---|---|---|
| Change lead time | Committed to version control → deployed in production | First commit of the change → first production deployment whose sha contains the merge (`git merge-base --is-ancestor <merge> <deploy-sha>`) |
| Deployment frequency | Deployments per period, or time between them | `gh api "repos/$O/$R/deployments?environment=production&per_page=100" --paginate --jq '.[].created_at'`, or tags `git for-each-ref refs/tags/deploy/prod --format='%(creatordate:iso-strict)'` |
| Failed deployment recovery time | A failing deployment → recovered | Failing deployment → next deployment that contains the fix or revert |
| Change fail rate | Share of deployments that need immediate intervention | Deployments followed by a `Revert "…"` commit or a commit with an `Incident:` trailer before the next planned deployment |
| Deployment rework rate | Share of deployments that are unplanned and caused by a production incident | Deployments whose included commits carry an `Incident:` trailer: `git log $PREV..$SHA --format='%(trailers:key=Incident,valueonly)' \| grep -c .` |

Throughput is lead time, deployment frequency and recovery time; instability is change fail rate and rework rate. Report them together, per service. Trailers need a recent git; deployment times must come from the deployments API or signed deploy tags, because git alone doesn't know when something shipped.

## Pitfalls

- **Targets.** Once survival rate or time-to-first-review is a goal, it stops measuring anything. Publish trends and discuss outliers instead.
- **Means.** Use median and p90.
- **Counting trivial work.** Trivial changes have no change folder by design; don't let them inflate or deflate per-change figures. Segment by tier.
- **Mixing agent and human changes.** Filter by branch prefix or the `Co-Authored-By` trailer before comparing.
- **Committer dates after rebase.** Use `%aI` for stage timestamps and `%cI` only for merge commits on the default branch.
- **Unlinked incidents.** Rework rate and repeat-incident counts need the `Incident:` trailer and a class on each incident. Without them, the lagging indicators for Maintain cannot be computed.
- **Treating a flat number as failure.** Flat policy findings usually mean the skill isn't triggering; check `skill_activated` before rewriting the policy.
