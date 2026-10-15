# Guide

Setting up the skill in a repo, then using it. Every `sdlc.json` key is in [configuration.md](configuration.md).

## Set up

Step by step, from nothing to a working repo. Run it inside the repo you want to put under the process.

1. **Install the skill.** Either per repo or per user:
   ```sh
   git clone https://github.com/yuri-geronimus/ai-native-sdlc <repo>/.claude/skills/ai-native-sdlc   # this repo only
   git clone https://github.com/yuri-geronimus/ai-native-sdlc ~/.claude/skills/ai-native-sdlc        # all your repos
   ```
   Check that `python3 --version` is 3.9 or later and `git` is on PATH. Claude Code must be 2.1.269 or later (`claude --version`).
2. **Start Claude Code in the repo** and ask it to preview the setup, or run it yourself (adjust the path for a user-level install):
   ```sh
   python3 .claude/skills/ai-native-sdlc/scripts/sdlc.py init --dry-run
   ```
   Nothing is written. It lists the files it would create and the test commands it detected.
3. **Install.**
   ```sh
   python3 .claude/skills/ai-native-sdlc/scripts/sdlc.py init --claude-md
   ```
   `init` is idempotent: running it again keeps your edits. `--force` overwrites the installed subagents; `--with-ci` also copies the GitHub workflows.
4. **Edit `.claude/sdlc.json`** (next section). At minimum set `verify_commands` and `default_branch`.
5. **Commit** `.claude/`, `REVIEW.md`, `changes/` and the CLAUDE.md change, so the whole team gets the same hooks.
6. **Restart Claude Code** so it picks up the hooks, then run `/ai-native-sdlc status`. It should report no active change and the next action.
7. **GitHub (optional, needed for separation of duties).** Add CODEOWNERS for `changes/**`, turn on branch protection, and require approval of the most recent push. See [references/adoption.md](../references/adoption.md).

Uninstall: delete the hook entries from `.claude/settings.json`, `.claude/agents/sdlc-*.md`, `.claude/sdlc.json` and the skill folder. `changes/` stays as your history.

## Day-to-day use

You describe work in plain language. The skill loads itself, or you call it with `/ai-native-sdlc <mode> [id]`.

| You want to | Say or run |
|---|---|
| Start from an idea, ticket or incident | "I have an idea: …", "Customers want …", "Here's ticket PROJ-123" |
| Fix a bug test-first | "Fix this bug: …" (needs a plan; the first task is a failing test) |
| See where things are | `/ai-native-sdlc status` |
| Check artifacts | `/ai-native-sdlc verify`, or `sdlc.py validate <id>` |
| Review before a PR | `/ai-native-sdlc review`, or `sdlc.py review-local` when `review.mode` is `isolated` |
| Prepare a release | `/ai-native-sdlc ship <version>`. You get a hand-off; Claude never deploys. |
| Judge a metric spike | "Is this spike real?" or `bands.py check` |
| Lead time, rework | `sdlc.py metrics` |

**What a change looks like**

1. Claude classes the request into a tier and creates `changes/<id>/intent.md`.
2. At each gate Claude shows a gate card and asks you: **Approve**, **Request changes** or **Reject**. Type your name when asked; it goes in the audit log. Approve only what you have read.
3. For standard and high-risk changes the order is intent, spec, plan. Claude then builds, runs `verify_commands`, and opens a PR with the evidence.
4. Editing an approved artifact above `## Change log` voids its approval, so you are asked again.

**If something is blocked**

- *"No source edits before the plan is approved"*: run `/ai-native-sdlc status` and finish the open gate. If the work really is trivial, say so and Claude will re-class it (the tier can only go up, never down, once artifacts exist).
- *"Production gate"*: working as designed. Set `production_gate.approval_check`, or ask the release manager.
- *Stop hook says tests haven't run*: Claude must run `verify_commands` after its last edit. If a command is wrong for this repo, fix it in sdlc.json.
- *Guards seem to do nothing*: check that the hooks are in `.claude/settings.json`, that you restarted Claude Code, and that `hooks_enabled` is `true`. `.claude/sdlc-state/hook-log.jsonl` records every decision.
- *Scripts in CI or `claude -p`*: set `SDLC_NONINTERACTIVE=1`, otherwise the automation's prompt counts as a human turn. `approve` refuses in headless runs by design.

## Review in CI

`claude-review.yml` reviews every PR automatically, so nobody launches it. It runs the Bugs, Security and Compliance passes against REVIEW.md, spec.md and plan.md, posts inline comments, and ends with one summary comment. It never approves or requests changes.

1. Copy the workflows: `python3 .claude/skills/ai-native-sdlc/scripts/sdlc.py init --with-ci` (existing files are skipped).
2. Install the Claude GitHub App on the repo (`/install-github-app` or https://github.com/apps/claude).
3. Add the secret `ANTHROPIC_API_KEY` (or `CLAUDE_CODE_OAUTH_TOKEN`, or a Bedrock, Vertex or Foundry setup; see the comments in the workflow).
4. Commit the skill, `.claude/agents/` and `REVIEW.md`. The CI run reads them from the checkout.
5. Open a PR. Drafts and fork PRs are skipped.

Optional repository variables: `SDLC_REVIEW_GATE=true` fails the job on Important findings (make it a required check to block merges); `SDLC_REVIEW_MODE=managed` skips the self-hosted review and only gates on Anthropic's managed Code Review. Each run is capped at 60 turns and $10. Keep a CODEOWNERS approval in branch protection: the review is not an approver. Engine choice and details: [stage-5-deploy.md](../references/stage-5-deploy.md).

To review locally instead, set `review.mode` to `isolated` in `sdlc.json` and run `sdlc.py review-local`.
