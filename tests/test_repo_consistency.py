"""Guards for values that are easy to break by hand: the SKILL.md frontmatter limits and the
Claude Code version pins repeated across docs and workflows."""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
import sdlc  # noqa: E402
import sdlc_lib as L  # noqa: E402

VERSION = re.compile(r"\d+\.\d+\.\d+")


def _tuple(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def _minimum() -> str:
    fm = L.parse_frontmatter((SKILL / "SKILL.md").read_text(encoding="utf-8"))[0]
    return VERSION.search(str(fm["compatibility"])).group(0)


class FrontmatterLimitsTest(unittest.TestCase):
    def setUp(self):
        self.fm = L.parse_frontmatter((SKILL / "SKILL.md").read_text(encoding="utf-8"))[0]

    def test_description_fits_limit(self):
        d = str(self.fm["description"])
        self.assertLessEqual(len(d), 1024, "description is %d chars" % len(d))
        self.assertNotIn(": ", d)
        self.assertFalse(set("<>") & set(d))

    def test_allowed_tools_are_bash_rules(self):
        rules = re.findall(r"Bash\([^)]*\)", str(self.fm["allowed-tools"]))
        self.assertTrue(rules)
        rest = re.sub(r"Bash\([^)]*\)", "", str(self.fm["allowed-tools"])).strip()
        self.assertEqual(rest, "", "allowed-tools has text outside Bash(...) rules")


class VersionPinsTest(unittest.TestCase):
    def test_workflow_pins_agree_and_meet_minimum(self):
        pins = {}
        for p in (SKILL / "assets" / "github").glob("*.yml"):
            m = re.search(r'CLAUDE_CODE_VERSION:\s*"([^"]+)"', p.read_text(encoding="utf-8"))
            if m:
                pins[p.name] = m.group(1)
        self.assertTrue(pins)
        self.assertEqual(len(set(pins.values())), 1, pins)
        for name, v in pins.items():
            self.assertGreaterEqual(_tuple(v), _tuple(_minimum()), name)

    def test_docs_state_the_same_minimum(self):
        minimum = _minimum()
        for rel in ("README.md", "docs/guide.md", "docs/how-it-works.md"):
            text = (SKILL / rel).read_text(encoding="utf-8")
            found = re.findall(r"Claude Code (\d+\.\d+\.\d+) or later|must be (\d+\.\d+\.\d+) or later", text)
            self.assertTrue(found, rel)
            for a, b in found:
                self.assertEqual(a or b, minimum, rel)


class PluginDescriptionTest(unittest.TestCase):
    def test_marketplace_matches_built_plugin(self):
        m = json.loads((SKILL / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(m["plugins"][0]["description"], sdlc.PLUGIN_DESCRIPTION)


if __name__ == "__main__":
    unittest.main()
