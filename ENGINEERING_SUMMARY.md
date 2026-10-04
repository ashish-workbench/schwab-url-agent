# Engineering summary

## Plan and rationale

The brief weights orchestration as the differentiator, so the URL shortener is deliberately small and
the effort went into a governed execution engine that can take *any* of the three requirement types
to a reviewable outcome. Work was sequenced as follows:

1. **Service baseline.** FastAPI + SQLite with validation, atomic click counting, expiry and an additive migration.
2. **Engine.** Validated DAG; freshness-based scheduling; parallel batches with a join barrier; entry and exit gates; retry, fallback, skip and rollback; safe-stop; approvals with pause and resume; audit log; metrics.
3. **Agents that do real work.** Normalisation, decomposition, AST impact analysis, codegen into an isolated workspace, a real pytest run, policy review, docs, and a release checklist with AC → test traceability.
4. **Scenarios.** The three required scenarios, plus five resilience scenarios that inject faults into the same pipeline.
5. **Verification.** 64 tests: service unit and API tests, engine unit tests on synthetic graphs, policy and reasoning tests, and end-to-end scenarios.

## Artifacts

| Artifact | Location |
|---|---|
| Runnable service | `app/`, run with `uvicorn app.main:app` |
| API / schema definition | OpenAPI at `/docs`; generated `docs/API.md` per run; `design.api_contract` and `design.schema` in each report |
| Tests | `tests/test_store.py`, `tests/test_api.py` (service); `tests/test_engine.py`, `tests/test_policy_reasoning.py`, `tests/test_scenarios.py` (orchestrator) |
| Per-run outputs | `runs/<id>/report.md`, `audit.jsonl`, `release/change.patch`, `release/CHECKLIST.md` |
| Sample reports | `docs/scenarios/*.md` |
| Architecture | `docs/ARCHITECTURE.md` |

## Validation and risk control

- **Gates.** Entry gates check required upstream outputs. Exit gates compile generated code and enforce release checks. The policy hook runs on every workspace-mutating stage.
- **Real execution.** The `test` stage runs pytest in the generated workspace with an isolated config and parses the JUnit XML. Release requires every acceptance criterion to map to at least one passing test. This is relaxed to a warning only in degraded fallback mode, and that is shown to the approver.
- **Guardrails.** Secrets (SEC-001), dangerous primitives (SEC-002), unvalidated redirect targets (SEC-003), PII columns (CMP-001), out-of-scope files (CHG-001), destructive SQL (CHG-002). Violations block without retry and roll back.
- **Safety stops.** Operator STOP file, attempt budget, wall-clock budget, and a re-plan oscillation cap.

| Risk / failure scenario | Control |
|---|---|
| Shortener used as an open redirector for phishing | http/https allow-list, credentials in URL rejected, 2 KB cap, default 30-day expiry in the regulated variant. Abuse reporting and domain reputation are out of scope. |
| Code enumeration | 7 characters from 62 symbols via `secrets`, about 3.5e12 codes. Rate limiting belongs at the gateway. |
| Lost or duplicated clicks under concurrency | Single conditional `UPDATE` (atomic per row). SQLite serialises writers; Postgres is needed for multi-node. |
| Schema change breaks existing links | Additive nullable column, migration test (AC7), human approval gate, rollback by redeploying the previous build. |
| Agent over-reach or scope creep | CHG-001 change-control against the design's approved file set; `policy-block` scenario. |
| Flaky infrastructure | Bounded retry with backoff; MTTR tracked. |
| Requirement changes mid-flight | Hash-based re-plan; approvals bound to inputs are invalidated automatically. |
| Runaway agent loop | Attempt, wall-clock and revision budgets → safe-stop. |

## Trade-offs

- **Deterministic agents rather than live LLM calls.** This proves the governance model reproducibly, with no keys and no flake. The cost is that generation quality is template-bound. The agent interface is the extension point, and the gates, policies and real test run are designed to catch a non-deterministic agent's mistakes.
- **Workspace snapshots rather than git worktrees.** Simpler and portable (works the same on Windows). It costs a disk copy per run and doesn't integrate with PR tooling.
- **Threads rather than processes or a queue.** Stages are I/O-bound (subprocess, disk), and an in-process pool keeps the barrier semantics easy to test. A distributed version would need a durable queue and leases.
- **Regex and AST policies rather than a policy engine (OPA).** Transparent and tested, but not exhaustive.

## Assumptions

- "Analytics" means an aggregate click counter per link, not an event pipeline or per-user tracking.
- "Safe for a bank" means a URL allow-list, no credentials, length cap, default expiry, and no PII. It does not mean fraud ML.
- Default TTL of 30 days in the regulated variant. Flagged for product-owner confirmation in the sign-off gate.
- Humans own the release decision. Agents produce a patch and checklist and never merge.

## Limitations

- No authentication, multi-tenancy or rate limiting on the service.
- Single-node SQLite. Horizontal scale needs Postgres and a cache in front of redirects.
- The codegen agent covers this domain's feature flags (create, redirect, analytics, expiry). Other requirements are normalised and planned, but implementation would need a model-backed agent.
- Approval identity is whatever the approver reports (OS user for console). There is no SSO or signature.
- The audit log is append-only by convention, not tamper-evident (no hash chain or WORM storage).
