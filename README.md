# URL shortener, agentic SDLC prototype

Schwab interview prototype. The shortener is the subject. The graded surface is the orchestrator: dependency graph, parallel implement fan-out, human release gate, bounded retry, rollback, safe-stop, policy checks, decision log, and re-plan when normalize changes.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scenarios/run_all.py
uvicorn app.main:app --reload
pytest -q
```

`POST /links {"url":"https://example.com"}` then `GET /{code}` returns 302. Stats at `GET /links/{code}/stats`.

## Architecture

- `app/` FastAPI + SQLite. Create, redirect, click count, expiry. http(s) only. No open redirect, no credentials in URL.
- `orchestrator/engine.py` explicit graph. `implement_api` and `implement_tests` run in a parallel batch and join at `test`. Release is a human gate (demo auto-approves; `auto_approve=False` pauses).
- Decision log is append-only on the run. Metrics: success, retries, rollbacks, elapsed ms, crude MTTR.
- Agents are deterministic role functions behind the same interface a model call would use. No API key, so the demo runs offline.

## Scenarios

1. Greenfield: create, redirect, clicks.
2. Brownfield: expiry on an existing shortener. Design stage lists impacted modules before the edit.
3. Ambiguous: "smarter and safe for a bank" is normalized to scheme allow-list, 30-day default ttl, no PII. Fraud ML is recorded as rejected.

## Testing

Unit tests cover code create, scheme rejection, expiry. Orchestrator tests cover the three scenarios, one retry recovery, the approval pause, and re-plan.

## Limitations

In-memory-ish SQLite file, no auth, no distributed lock, agents do not call a model, rollback clears the artifact list rather than a git worktree. Enough to show the control flow. Not a platform.
