"""Config loading and interview-plan generation.

The plan is the interview: an opener plus N mains, each with a scripted
two-deep dive-down chain and canned fallback probes. With an LLM provider the
plan is generated from the candidate's own resume (the way real AI screeners
work); offline it falls back to a solid generic plan.
"""

import hashlib
import json
import pathlib
import re

from providers import ProviderUnavailable

ROOT = pathlib.Path(__file__).parent


# --------------------------------------------------------------------------
# Config: PyYAML if present, else a minimal parser that covers the shipped
# config schema (flat keys, one level of "- " lists, # comments).
# --------------------------------------------------------------------------

def _mini_yaml(text):
    data = {}
    current_list = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("- ") and current_list is not None:
            data[current_list].append(line.lstrip()[2:].strip().strip("'\""))
            continue
        m = re.match(r"^([A-Za-z_][\w]*)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val == "":
            data[key] = []
            current_list = key
        else:
            current_list = None
            if val.lower() in ("true", "false"):
                data[key] = val.lower() == "true"
            else:
                try:
                    data[key] = int(val)
                except ValueError:
                    data[key] = val.strip("'\"")
    return data


def load_config(path=None):
    p = pathlib.Path(path) if path else ROOT / "config.yaml"
    if not p.exists():
        p = ROOT / "config.yaml.example"
    text = p.read_text()
    try:
        import yaml  # optional dependency
        cfg = yaml.safe_load(text)
    except ImportError:
        cfg = _mini_yaml(text)
    cfg.setdefault("role", "Software Engineer")
    cfg.setdefault("interviewer_name", "Avery")
    cfg.setdefault("provider", "none")
    cfg.setdefault("port", 8477)
    cfg.setdefault("questions", 7)
    cfg.setdefault("session_minutes", 16)
    cfg.setdefault("profile", "profiles/example-role")
    cfg.setdefault("focus_areas", [])
    return cfg


def read_resume(cfg):
    for candidate in (ROOT / "profile" / "resume.txt",
                      ROOT / cfg["profile"] / "resume.txt"):
        if candidate.exists():
            return candidate.read_text()[:4000]
    return ""


# --------------------------------------------------------------------------
# Plan generation
# --------------------------------------------------------------------------

PLAN_PROMPT = """You are designing a mock screening interview that imitates modern
AI interviewers (resume-grounded, conversational, adaptive). Produce the interview
plan as STRICT JSON only — no markdown fences.

Role being screened for: {role}
Interviewer name: {name}
Number of main questions: {n}
Focus areas requested: {areas}

Candidate resume:
{resume}

Rules for the plan:
- Question 1 is a warm opener: greet the candidate by first name if the resume
  gives one, brief icebreaker, then ask for a walkthrough of their background.
- Most mains must QUOTE the resume ("Your resume says ... — tell me about that"),
  picking the most probeable claims: impressive metrics, named technologies,
  old or thin bullets, and anything a screen for this role must cover.
- Every main gets "chain": exactly 2 scripted dive-down follow-ups — first go a
  level deeper on the expected answer (mechanisms, numbers, decisions), then
  probe a likely omission.
- Include one behavioral main about following strict guidelines or a
  deadline-vs-quality collision, and one communication main (explain a concept
  from their field to a non-technical listener).
- The last main is a closing: motivation for the role, then "Is there anything
  else you'd like me to know?" as its chain.
- Every main also gets "canned": 2 generic fallback probes usable offline.

JSON shape:
{{"mains": [{{"area": "<short label>", "q": "<spoken question>",
  "chain": ["<followup 1>", "<followup 2>"], "canned": ["<p1>", "<p2>"]}}]}}
"""


def _generic_plan(cfg):
    name = cfg["interviewer_name"]
    role = cfg["role"]
    mains = [
        {"area": "Background",
         "q": "Hi, my name is %s. Thank you for joining the interview. How's your day going so far?" % name,
         "chain": ["Great. Let's begin. Give me the two-minute version of your career — most recent role first, and for each role tell me what you personally owned."],
         "canned": ["Which of those roles shaped how you work the most, and why?",
                    "What's the through-line connecting those roles?"]},
        {"area": "Project deep-dive",
         "q": "Tell me about the most technically challenging project on your resume. What was the problem, what was your specific contribution, and what was the outcome?",
         "chain": ["What are the specific numbers behind that outcome — and how were they measured?",
                   "What went wrong along the way that you haven't mentioned yet?"],
         "canned": ["What would falsify the success claim you just made?",
                    "Who else worked on it, and what part was only yours?"]},
        {"area": "Technical judgment",
         "q": "Pick the most important technical decision you made in that project. Walk me through the alternatives you rejected and why.",
         "chain": ["What would have to change about the requirements for the rejected option to win?",
                   "What did that decision cost you later?"],
         "canned": ["How did you validate the decision after shipping?",
                    "What did you not know at decision time that you know now?"]},
        {"area": "Debugging",
         "q": "Tell me about a hard production problem you personally debugged.",
         "chain": ["How was it detected in the first place?",
                   "What did you change afterward so it couldn't happen again?"],
         "canned": ["What was the actual root cause, in one sentence?",
                    "How long did diagnosis take, and what was the turning point?"]},
        {"area": "Following guidelines",
         "q": "Tell me about a time you had to follow a strict, complex set of guidelines exactly — even where you disagreed with them.",
         "chain": ["What did you do at the moment the guidelines felt wrong?",
                   "How would you handle a case the guidelines genuinely don't cover, on a deadline?"],
         "canned": ["What makes guidelines followable versus ignorable in practice?",
                    "When is escalating better than deciding?"]},
        {"area": "Communication",
         "q": "Explain a technical concept from your field to someone smart who has never studied it.",
         "chain": ["What does your explanation deliberately leave out — and what breaks if the listener never learns it?"],
         "canned": ["Give me the same explanation in one sentence."]},
        {"area": "Closing",
         "q": "Last question. Why do you want the %s role — and what should we remember about you?" % role,
         "chain": ["Is there anything else you'd like me to know?"],
         "canned": []},
    ]
    n = max(3, int(cfg["questions"]))
    if n < len(mains):
        keep = [0, 1] + list(range(len(mains) - (n - 2), len(mains)))
        mains = [mains[i] for i in sorted(set(keep))]
    return {"mains": mains, "source": "generic"}


def _parse_plan(text):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    data = json.loads(m.group(0))
    mains = data["mains"]
    assert isinstance(mains, list) and len(mains) >= 3
    for q in mains:
        q["area"] = str(q.get("area", "Question"))
        q["q"] = str(q["q"])
        q["chain"] = [str(c) for c in q.get("chain", [])][:3]
        q["canned"] = [str(c) for c in q.get("canned", [])][:2]
    return {"mains": mains, "source": "llm"}


def build_plan(cfg, provider):
    """Generate (or reuse cached) interview plan for the current resume+config."""
    resume = read_resume(cfg)
    key = hashlib.sha256(
        (resume + cfg["role"] + str(cfg["questions"]) + provider.name).encode()
    ).hexdigest()[:16]
    cache = ROOT / "sessions" / ("plan-%s.json" % key)
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    plan = None
    if resume:
        try:
            raw = provider.complete(PLAN_PROMPT.format(
                role=cfg["role"], name=cfg["interviewer_name"],
                n=cfg["questions"], areas=", ".join(cfg["focus_areas"]) or "interviewer's choice",
                resume=resume), timeout=90)
            plan = _parse_plan(raw)
        except (ProviderUnavailable, Exception):
            plan = None
    if plan is None:
        plan = _generic_plan(cfg)
    plan["role"] = cfg["role"]
    plan["interviewer_name"] = cfg["interviewer_name"]
    plan["session_minutes"] = cfg["session_minutes"]
    cache.parent.mkdir(exist_ok=True)
    cache.write_text(json.dumps(plan, indent=2))
    return plan
