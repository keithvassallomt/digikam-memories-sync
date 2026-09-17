"""Runs the interface's own formatting module under Node.

There is no JavaScript test framework in this project, and adding one for a
handful of pure functions would cost more than it returns. Node is already
required to check the modules parse, so the same tool runs them here. The
tests skip cleanly where it is missing.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "digimem" / "web" / "js" / "format.js"


def run_js(body: str) -> dict:
    """Import format.js, run `body`, and return what it prints as JSON."""
    script = f"import * as format from {json.dumps(MODULE.as_uri())};\n{body}\n"
    finished = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    if finished.returncode != 0:
        raise AssertionError(finished.stderr.strip())
    return json.loads(finished.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class DayLabelTest(unittest.TestCase):
    """A run from yesterday evening must not be labelled today."""

    def labels(self, *timestamps: str) -> dict:
        calls = ", ".join(
            f"{json.dumps(stamp)}: format.dayLabel({json.dumps(stamp)})"
            for stamp in timestamps
        )
        return run_js(f"console.log(JSON.stringify({{{calls}}}))")

    def test_calendar_days_not_elapsed_hours(self):
        # Built relative to now so the test does not rot.
        found = run_js("""
const day = 86400000;
const at = (offsetDays, hour) => {
  const m = new Date(Date.now() - offsetDays * day);
  m.setHours(hour, 0, 0, 0);
  // The interface receives SQLite timestamps, which are UTC and unmarked.
  return m.toISOString().replace('T', ' ').slice(0, 19);
};
console.log(JSON.stringify({
  thisMorning: format.dayLabel(at(0, 9)),
  lastNight: format.dayLabel(at(1, 20)),
  yesterdayMorning: format.dayLabel(at(1, 9)),
}));
""")
        self.assertEqual(found["thisMorning"], "today")
        self.assertEqual(
            found["lastNight"], "yesterday",
            "an evening run was reading as today because the gap was under a day")
        self.assertEqual(found["yesterdayMorning"], "yesterday")

    def test_nothing_reads_as_a_day_label(self):
        self.assertEqual(self.labels("")[""], "")
        self.assertEqual(self.labels("not a date")["not a date"], "")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class CountingTest(unittest.TestCase):
    def test_plural_agrees_with_its_number(self):
        found = run_js("""
console.log(JSON.stringify({
  one: format.plural(1, 'change', 'changes'),
  many: format.plural(7855, 'change', 'changes'),
  none: format.plural(0, 'change', 'changes'),
}));
""")
        self.assertEqual(found["one"], "1 change")
        self.assertEqual(found["many"], "7,855 changes")
        self.assertEqual(found["none"], "0 changes")

    def test_percent_handles_an_unknown_total(self):
        found = run_js("""
console.log(JSON.stringify({
  half: format.percent(50, 100),
  unknown: format.percent(5, 0),
  over: format.percent(120, 100),
}));
""")
        self.assertEqual(found["half"], 50)
        self.assertIsNone(found["unknown"])
        self.assertEqual(found["over"], 100)


if __name__ == "__main__":
    unittest.main()
