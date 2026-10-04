# Engineering summary

## Plan

Ship a runnable URL shortener and a separate orchestrator that can execute greenfield, brownfield, and ambiguous requirements through one dependency graph. Keep the product thin so the control flow is what a reviewer runs.

## Decisions

- Graph, not a chain. Implement API and tests fan out after design and join at the test gate.
- Release is high-impact. It stops unless approved. Demo mode records an auto-approval so the script finishes.
- Ambiguous bank wording is normalized, not "solved" with a model. Assumptions sit on the run context.
- Brownfield design records impacted modules (`store` expiry column, redirect handler, expiry test) before any implement stage.
- Policy rejects secrets, PII in analytics, and open redirects inside the stage runner.

## Risks and trade-offs

- Deterministic agents prove governance, not generation quality. A live model can be slotted behind the same `Agent` callable; it was left out so the run has no key and no flake.
- Rollback here drops the artifact set and safe-stops. A real run would reset a worktree.
- SQLite click increments are not globally unique under multi-writer load. Acceptable for the prototype.
- Re-plan marks downstream stale and reruns them. It does not diff file hunks.

## Assumptions

- 2–3 day brief, submitted as a one-sitting prototype.
- "Analytics" means a click counter, not an event pipeline.
- "Safe for a bank" means scheme allow-list, expiry, no PII. It does not mean fraud detection.
- Human owns the release gate. Agents do not merge.

## Not built

Custom aliases, auth, rate limits, QR, multi-region, a real policy engine, Kafka analytics, git-level rollback, live LLM calls.
