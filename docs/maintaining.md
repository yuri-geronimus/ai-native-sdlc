# Maintaining the skill

For people who change the skill itself.

Run the unit tests (about two minutes):

```
cd .claude/skills/ai-native-sdlc
python3 -m unittest discover -s tests -t .
/usr/bin/python3 -m unittest discover -s tests -t .   # Python 3.9
```

After editing the SKILL.md description, re-run the trigger evals in a throwaway repo that contains the skill. This makes real `claude -p` calls and costs a few dollars:

```
python3 evals/run_trigger_evals.py --help
```

Other points:

- **Tested with:** Claude Code 2.1.294 (checked 2026-10-08); the minimum is 2.1.269 (SKILL.md `compatibility`). The three CI workflows pin `CLAUDE_CODE_VERSION` to the tested version. When you re-check against a newer Claude Code, bump the pins and this line together; `tests/test_repo_consistency.py` fails if the pins disagree or fall below the minimum.

- **Description limits:** keep it under 1,024 characters, with no `: ` and no angle brackets. Check that it parses as YAML.
- **Re-check the facts:** the Claude Code facts the skill relies on are marked where they're unverified. Re-check them when Claude Code changes major behaviour (hooks, permissions, `claude -p` flags).
- **Editing in a repo with the hooks installed:** `self_protect` blocks agent edits to the skill folder there. Edit the files yourself, or set `"self_protect": false` in `.claude/sdlc.json` by hand.

## Releasing

1. Move the Unreleased items in [CHANGELOG.md](../CHANGELOG.md) under a new version heading and set `metadata.version` in SKILL.md to match.
2. Run the unit tests, and the trigger evals if the SKILL.md description changed.
3. Commit, then tag and push: `git tag v<version> && git push origin v<version>`. The `publish-plugin` workflow checks that the tag matches the SKILL.md version, builds the plugin with `package-plugin` and force-pushes it to the `plugin` branch, which `.claude-plugin/marketplace.json` installs from.
4. Create the GitHub release from the tag, with the changelog section as the notes.
5. Bump `version` in `.claude-plugin/marketplace.json` to match.

Check a plugin build locally with `python3 scripts/sdlc.py package-plugin --out <dir outside the repo>` and `claude plugin validate <dir> --strict`.

## Repo settings

Set once in GitHub: enable Discussions, add the labels in [.github/labels.yml](../.github/labels.yml), set the description and topics (`claude-code`, `claude-skill`, `claude-code-plugin`, `ai-agents`, `sdlc`, `governance`), and upload a social preview image.
