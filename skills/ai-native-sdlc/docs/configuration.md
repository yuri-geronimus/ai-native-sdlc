# Configuring sdlc.json

`.claude/sdlc.json` is created by `init` from [assets/templates/sdlc.json](../assets/templates/sdlc.json). It is plain JSON, and you edit it by hand: while `self_protect` is on, Claude can't change it. A key you leave out gets the default shown. If the file doesn't parse, the guards fail open and say so in the hook log, so check it with `python3 -m json.tool .claude/sdlc.json` after editing.

### The keys most repos set

| Key | Default | What to put |
|---|---|---|
| `verify_commands` | `[]` (filled by `init` when it detects tests) | Commands that prove the repo is healthy, run in order, e.g. `["make test", "npm run lint"]`. The Stop hook blocks "done" until they have run after the last edit, and the verifier subagent runs them. Empty means the Stop check does nothing. |
| `default_branch` | `"main"` | Base for diffs, drift, review and PRs. Set it to `master`, `develop`, etc. if that is your trunk. |
| `test_globs` | common test paths | Which files count as tests. Fix mode makes these read-only, and the plan guard treats them separately from source. Add your own layout, e.g. `"spec/**"`. |
| `protected_paths` | `[]` | Globs Claude may never edit, e.g. `["infra/prod/**", "migrations/applied/**"]`. |
| `plan_guard_exempt` | `[]` | Globs that need no approved plan to edit and no verification run, e.g. `["docs/**", "*.md"]`. |
| `production_gate.approval_check` | `""` | Usually leave empty: every production action is denied and your release pipeline deploys. Set it only if Claude must run a step after authorisation (see below). |

### The whole file

| Key | Default | Meaning |
|---|---|---|
| `changes_dir` | `"changes"` | Where `changes/<id>/` folders live. |
| `source_of_truth` | `"repo"` | `"repo"`, `"legacy"` or `"linkage"`, or an object per artifact (`{"intent": "legacy", "plan": "repo"}`). It documents the choice for people and CI; `sdlc.py` doesn't change behaviour on it. See [governance.md](../references/governance.md). |
| `record_id_pattern` | `""` | Regex a `--record-id` must match, e.g. `"^CHG[0-9]{7}$"` or `"^PROJ-[0-9]+$"`. |
| `hooks_enabled` | `true` | `false` switches off the fail-open guards (plan guard, fix mode, stop-verify, self-protect). It never switches off the production gate. |
| `self_protect` | `true` | Claude can't edit sdlc.json, settings, the subagents or the skill folder. Set to `false` by hand when you need to edit the skill itself. |
| `plan_guard` | `true` | No source edits before the plan is approved. |
| `verify_on_stop` | `true` | Block "done" until `verify_commands` ran after the last edit. |
| `post_edit` | `[]` | Checks to run after each edit: `[{"glob": "**/*.py", "run": "ruff check {file}"}]`. A non-zero exit is fed back to Claude. `{file}` is passed safely through an environment variable. |
| `review.mode` | `"off"` | `"isolated"` turns on `sdlc.py review-local`, a clean-context review of HEAD. |
| `review.passes` | Bugs, Security, Compliance | Which review passes run. |
| `review.project_docs` | `false` | `true` lets the reviewer see CLAUDE.md and project docs; `false` strips them. |
| `review.model`, `review.timeout_s` | `""`, `900` | Model override and per-pass timeout in seconds. |
| `production_gate.bash_patterns` | kubectl/helm/terraform/deploy regexes | Regexes that mark a shell command as a production action. Add your own deploy tool, e.g. `"\\bcdk\\s+deploy\\b.*prod"`. |
| `production_gate.mcp_tools` | `deploy_prod`, `*production*` | Regexes for MCP tool names treated as production actions. |
| `production_gate.approval_timeout_s` | `20` | Seconds before the approval check counts as failed. |
| `runbooks` | `{}` | Pre-approved, model-free actions the control-band wrapper may run. See [automation.md](../references/automation.md). |
| `evals_dir` | `"evals"` | Where `run_evals.py` looks for eval cases. |

### Example

```json
{
  "default_branch": "develop",
  "verify_commands": ["npm test", "npm run lint", "npm run typecheck"],
  "test_globs": ["src/**/*.test.ts", "e2e/**"],
  "protected_paths": ["infra/prod/**"],
  "plan_guard_exempt": ["docs/**", "*.md"],
  "post_edit": [{"glob": "**/*.ts", "run": "npx prettier --check {file}"}],
  "review": {"mode": "isolated"},
  "production_gate": {
    "approval_check": "python3 tools/release_approved.py"
  }
}
```

### The production approval check

The recommended stance is the empty default: Claude prepares the release and the pipeline (a GitHub Environment `production` with required reviewers) deploys. Set `approval_check` only if Claude must execute a step after authorisation. Point it at a repo script that queries external evidence and exits 0 only when it exists ([stage-5-deploy.md](../references/stage-5-deploy.md)). It runs when Claude attempts a production action. Exit 0 allows it; anything else, a timeout or an empty value denies it. The check must read evidence from outside the session: a GitHub Environment approval, a change ticket in the "Implement" state, a release flag set by your pipeline. It receives `SDLC_CHANGE_ID`, `SDLC_TOOL` and `SDLC_COMMAND` in its environment, so a script can look up the ticket for the change. Never point it at something Claude or the person at the keyboard can set, such as an env var.
