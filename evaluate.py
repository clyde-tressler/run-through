"""Score a finished interview transcript and append to the mastery tracker.

Usage:
    python3 evaluate.py                      # newest transcript in transcripts/
    python3 evaluate.py transcripts/foo.json

Writes reports/eval-<timestamp>.md and appends one line to mastery.md.
Requires an LLM provider; in offline mode it writes a self-review worksheet
instead, so the drill loop still closes.
"""

import datetime
import json
import pathlib
import re
import sys

from planner import load_config
from providers import ProviderUnavailable, make_provider

ROOT = pathlib.Path(__file__).parent

RUBRIC = """Score the candidate on seven dimensions, each 1-5 with one sentence of
justification:
1. Technical depth — correctness and sophistication of technical content.
2. Specificity — concrete numbers, named tools, real decisions vs. generalities.
3. Communication — clarity, structure, signposting ("there are three parts...").
4. Completeness — answers land an ending; no trail-offs or abandoned questions.
5. Follow-up survival — did answers hold up when probed a level deeper?
6. Thinking aloud — pauses filled verbally; no long silences; question restated.
7. Time management — answers near 60-120s, no interruptions for overrun.

Then give: overall score (1-5, one decimal), the 3 strongest moments (quote them),
the 3 highest-value fixes (each with a concrete drill), and a one-line verdict."""

PROMPT = """You are grading a mock screening interview for a {role} role, the way an
AI interviewer's transcript-based evaluation would. Judge only what is in the
transcript — it is the sole artifact.

{rubric}

Transcript (speaker-tagged turns with per-answer timing metadata):
{transcript}

Format the whole evaluation as clean markdown. Begin with "## Overall: <score>/5"."""

WORKSHEET = """# Self-review worksheet (offline mode)

No LLM provider is configured, so score yourself — honestly — against the rubric.
Reread the transcript beside this file and fill in each line.

{rubric}

Transcript: {path}

| Dimension | Score (1-5) | Evidence |
|---|---|---|
| Technical depth | | |
| Specificity | | |
| Communication | | |
| Completeness | | |
| Follow-up survival | | |
| Thinking aloud | | |
| Time management | | |

**Three fixes to drill before the next session:**
1.
2.
3.
"""


def newest_transcript():
    files = sorted((ROOT / "transcripts").glob("transcript-*.json"))
    if not files:
        raise SystemExit("No transcripts found — finish an interview first.")
    return files[-1]


def render_transcript(data):
    lines = []
    for t in data.get("turns", []):
        who = "INTERVIEWER" if t.get("speaker") == "interviewer" else "CANDIDATE"
        meta = []
        if t.get("seconds") is not None:
            meta.append("%ss" % t["seconds"])
        if t.get("reason"):
            meta.append(t["reason"])
        if t.get("nudged"):
            meta.append("nudged")
        lines.append("%s [%s%s]: %s" % (who, t.get("type", ""),
                                        (", " + ", ".join(meta)) if meta else "",
                                        t.get("text", "")))
    for f in data.get("flags", []):
        lines.append("PROCTOR FLAG: %s at t=%smin" % (f.get("type"), f.get("t")))
    return "\n".join(lines)[:24000]


def main():
    cfg = load_config()
    path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else newest_transcript()
    data = json.loads(path.read_text())
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = ROOT / "reports" / ("eval-%s.md" % ts)
    report_path.parent.mkdir(exist_ok=True)

    provider = make_provider(cfg)
    report = None
    if provider.probe():
        try:
            report = provider.complete(PROMPT.format(
                role=data.get("role") or cfg["role"], rubric=RUBRIC,
                transcript=render_transcript(data)), timeout=120)
        except ProviderUnavailable:
            report = None
    if report is None:
        report = WORKSHEET.format(rubric=RUBRIC, path=path.name)

    report_path.write_text(report)

    overall = "?"
    m = re.search(r"Overall:\s*([0-9.]+)\s*/\s*5", report)
    if m:
        overall = m.group(1)
    tracker = ROOT / "mastery.md"
    if not tracker.exists():
        tracker.write_text("# Mastery tracker\n\n| Date | Overall | Report | Transcript |\n|---|---|---|---|\n")
    with tracker.open("a") as f:
        f.write("| %s | %s | %s | %s |\n" % (
            datetime.date.today().isoformat(), overall, report_path.name, path.name))

    print("Report: %s" % report_path)
    print("Tracker updated: mastery.md (overall: %s)" % overall)


if __name__ == "__main__":
    main()
