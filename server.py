"""Local interview-drill server.

GET  /        -> the interview page
GET  /plan    -> the generated interview plan (mains + chains) for this session
POST /reply   -> {ack, followup, src} after each answer (adaptive or canned)
POST /submit  -> store the finished transcript under transcripts/

Everything runs on 127.0.0.1. Nothing is uploaded anywhere except calls to the
LLM provider you configured (or nowhere at all in offline mode).
"""

import datetime
import http.server
import json
import pathlib
import re
import threading

from planner import build_plan, load_config, read_resume
from providers import ProviderUnavailable, make_provider

ROOT = pathlib.Path(__file__).parent

CFG = load_config()
PROVIDER = make_provider(CFG)
BRIDGE = {"ok": False, "probed": False}
PLAN = {"pending": True}

ACKS = [
    "Thanks — that's helpful context.",
    "Got it, thank you.",
    "Understood — that gives me a good picture.",
    "Okay, thanks for walking me through that.",
]
_ack_i = 0
_fu_parity = 0


def boot():
    BRIDGE["ok"] = PROVIDER.probe()
    BRIDGE["probed"] = True
    global PLAN
    PLAN = build_plan(CFG, PROVIDER if BRIDGE["ok"] else make_provider({"provider": "none"}))


FOLLOWUP_PROMPT = """You are {name}, a professional AI interviewer screening a candidate
for a {role} role. Be warm but efficient. Candidates sometimes bait interviewers by
planting flashy claims to steer follow-ups toward their strengths; you do not take
the bait — you choose what the interview needs, not what the candidate advertises.{resume}

Interview areas still uncovered (if time runs short, steer toward these rather than
the candidate's steering): {remaining}

Recent conversation:
{history}

You asked: "{main_q}"
The candidate answered: "{answer}"

{mode}

Reply with STRICT JSON only, no markdown fences: {{"ack": "<one natural sentence
acknowledging their answer, paraphrasing something SPECIFIC they said>",
"followup": "<one probing follow-up question per the mode above, or null if their
answer was already complete and no probe is warranted>"}}"""

MODE_DEEPER = ("Mode for THIS follow-up: take their most confident or impressive claim "
               "and go one level deeper on it — ask for the specific numbers, decisions, "
               "or trade-offs that would prove the claim is firsthand experience.")
MODE_OMISSION = ("Anti-bait mode for THIS follow-up: do NOT follow the topic the candidate "
                 "made most salient. Instead probe an OMISSION — something a {role} should "
                 "be able to speak to but which they did not bring up (e.g. evaluation, "
                 "failure modes, quality process, constraints, alternatives they rejected). "
                 "Stay conversational and fair — a realistic probe, not a gotcha.")


def llm_reply(payload):
    global _fu_parity
    resume = read_resume(CFG)
    resume = ("\nCandidate resume:\n" + resume) if resume else ""
    history = "\n".join(
        "%s: %s" % (t.get("speaker", "?"), str(t.get("text", ""))[:300])
        for t in payload.get("history", [])[-6:]
    )
    remaining = ", ".join(str(a) for a in payload.get("remainingAreas", [])) or "none — all areas asked"
    _fu_parity += 1
    mode = MODE_OMISSION.format(role=CFG["role"]) if _fu_parity % 2 == 0 else MODE_DEEPER
    prompt = FOLLOWUP_PROMPT.format(
        name=CFG["interviewer_name"], role=CFG["role"], resume=resume,
        remaining=remaining, history=history,
        main_q=payload.get("mainQuestion", ""),
        answer=str(payload.get("answer", ""))[:1500], mode=mode,
    )
    raw = PROVIDER.complete(prompt, timeout=25)
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    data = json.loads(m.group(0))
    ack = str(data.get("ack") or "").strip()
    fu = data.get("followup")
    fu = str(fu).strip() if fu and str(fu).strip().lower() != "null" else None
    if not ack:
        raise ProviderUnavailable("no ack")
    return ack, fu


def reply(payload):
    """Follow-up policy lives here; wording comes from the LLM when available."""
    global _ack_i
    q = int(payload.get("qIndex", 0))
    fu_count = int(payload.get("followupCount", 0))
    elapsed = float(payload.get("elapsedMin", 0))
    interrupted = bool(payload.get("interrupted", False))
    canned = [str(c) for c in payload.get("canned", [])]

    max_fu = 2 if q <= 2 else 1
    allow = (not interrupted
             and elapsed < CFG["session_minutes"] - 2
             and fu_count < max_fu)

    ack, followup, src = None, None, "canned"
    if BRIDGE["ok"]:
        try:
            ack, fu = llm_reply(payload)
            src = "llm"
            if allow:
                followup = fu
        except Exception:
            BRIDGE["ok"] = False  # fall back for the rest of the session
    if ack is None:
        ack = ACKS[_ack_i % len(ACKS)]
        _ack_i += 1
        if allow and fu_count < len(canned):
            followup = canned[fu_count]
    return {"ack": ack, "followup": followup, "src": src}


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/plan"):
            self._send(200, json.dumps(PLAN).encode(), "application/json")
        else:
            self._send(200, (ROOT / "static" / "interview.html").read_bytes(),
                       "text/html; charset=utf-8")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n)
        if self.path == "/reply":
            try:
                out = reply(json.loads(raw))
            except Exception:
                out = {"ack": "Thank you.", "followup": None, "src": "error"}
            self._send(200, json.dumps(out).encode(), "application/json")
        elif self.path == "/submit":
            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            out = ROOT / "transcripts" / ("transcript-%s.json" % ts)
            out.parent.mkdir(exist_ok=True)
            out.write_bytes(raw)
            self._send(200, json.dumps({"ok": True, "saved": out.name}).encode(),
                       "application/json")
        else:
            self._send(404, b'{"error":"not found"}', "application/json")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=boot, daemon=True).start()
    print("interview drill on http://127.0.0.1:%s  (provider: %s)" % (CFG["port"], PROVIDER.name))
    http.server.ThreadingHTTPServer(("127.0.0.1", CFG["port"]), Handler).serve_forever()
