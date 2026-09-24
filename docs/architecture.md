# Architecture notes

## Design constraints

1. **Local-first.** A resume is sensitive; transcripts of your own fumbling
   answers more so. Everything binds to 127.0.0.1 and writes to the repo
   directory. The only egress is the LLM provider the user explicitly
   configures.
2. **Zero required dependencies.** Python stdlib only (PyYAML optional). The
   barrier to "try it" should be one command.
3. **Degrade gracefully.** Every LLM-powered feature has a non-LLM fallback:
   plan generation → generic plan; adaptive follow-ups → canned probes;
   scored evaluation → self-review worksheet. `provider: none` is a fully
   working product.

## The turn-taking state machine (interview.html)

The interesting part of the client is deciding when the candidate is *done
talking* — real AI interviewers use a short silence window, and replicating
that pressure is the point of the tool.

- Recognition results (interim or final) timestamp `lastResultAt` and set
  `spokeStarted`.
- A 300 ms poll ends the turn when `now - lastResultAt > 2500 ms` *after*
  speech has begun. Silence before any speech instead earns a spoken nudge at
  10 s ("Take your time — go ahead"), whose duration is credited back to the
  answer clock.
- A hard interrupt fires at 2 minutes: the interviewer cuts in and the
  conversation moves on — with the partial answer recorded and labeled.
- Chrome's recognizer stops itself periodically; `onend` restarts it while
  `listening` is true. TTS and recognition are never active simultaneously
  (the interviewer's own voice would transcribe itself).

## Follow-up policy (server.py)

Wording comes from the LLM; *policy* stays in code so a chatty model can't run
the interview off the rails:

- Early mains allow up to 2 follow-ups, later mains 1.
- No follow-ups after an interrupt or inside the last two minutes.
- Scripted chains (the plan's two-deep dive-downs) run before any adaptive
  follow-up and are client-driven, so every planned probe fires even with the
  server offline.
- Adaptive follow-ups alternate modes per request: *go deeper on the
  candidate's most confident claim* vs. *probe an omission they didn't bring
  up*. The alternation is the anti-bait mechanism — candidates quickly learn
  they cannot steer every probe onto favorite territory.

## Plan generation (planner.py)

One LLM call at boot builds the whole interview from the resume + role config:
mains quoting resume bullets, each with a scripted chain and canned fallbacks.
Plans are cached in `sessions/` keyed by hash(resume + role + provider), so
re-running the server doesn't re-bill, and editing the resume regenerates.

## Evaluation (evaluate.py)

Transcript-only grading, mirroring how the real systems score: the transcript
is the sole artifact, judged on seven dimensions with quoted evidence and
concrete drills. The one-line-per-session `mastery.md` append is deliberately
crude — a flat file the user can read, edit, and diff.
