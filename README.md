# Agentic SDLC orchestrator: URL shortener

A working prototype that takes a requirement (well-defined, brownfield or ambiguous) and drives it
through a governed, graph-based SDLC: requirements → decomposition → design (with codebase reasoning)
→ parallel implementation → real test run → security/compliance review ∥ docs → release readiness, with
human approval checkpoints along the way. The URL shortener is the subject. The orchestrator is the product.

Every run produces a **reviewable engineering outcome**: generated code in an isolated workspace, a
unified diff (`release/change.patch`), a release checklist with acceptance-criteria → passing-test
traceability, an append-only audit log, and a markdown report with the executed graph.

## Quick start

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
python -m pytest                       # 64 tests: service, engine, policies, end-to-end scenarios
python -m orchestrator demo            # runs all 8 scenarios + pause/resume, prints metrics
```

Run the service that the orchestrator produced and promoted into `app/`:

```bash
uvicorn app.main:app --reload
curl -X POST localhost:8000/links -H "content-type: application/json" -d '{"url":"https://example.com"}'
curl -i localhost:8000/<code>          # 302, or 410 once expired
curl localhost:8000/links/<code>/stats
```

Interactive docs are at `http://localhost:8000/docs`.

## Driving the orchestrator

```bash
python -m orchestrator scenarios                         # list scenarios
python -m orchestrator run brownfield                    # prompts at each human gate
python -m orchestrator run ambiguous --approve pause     # pauses at the first gate, state saved to disk
python -m orchestrator resume <run_id> --approve prompt  # continue later, from any process
python -m orchestrator stop <run_id>                     # operator safe-stop before the next batch
python -m orchestrator run greenfield --requirement "Build a URL shortener whose links expire"
python -m orchestrator metrics                           # success rate, retry/rollback frequency, MTTR, p50/p95
```

Each run writes `runs/<run_id>/` containing `report.md`, `audit.jsonl`, `state.json`, `workspace/`,
`baseline/` and `release/{change.patch,CHECKLIST.md}`. Sample reports for every scenario are committed
in [`docs/scenarios/`](docs/scenarios/).

## Scenarios

| Scenario | What it demonstrates |
|---|---|
| `greenfield` | New service from an empty workspace. Contract-first: API and tests are generated in parallel, then joined at the test gate. |
| `brownfield` | Enhancement to the existing v1 service. AST-based impact analysis names the table, functions, routes and tests touched, plus an additive migration, *before* any edit. The migration needs human approval. |
| `ambiguous` | "Make our links smarter and safe for a bank." Vague terms become explicit interpretations, rejected options (ML fraud scoring and others) and testable acceptance criteria. A product-owner sign-off gate sits before design. |
| `flaky-test` | Transient test-runner failure recovered by bounded retry. MTTR recorded. |
| `fallback` | Test generator keeps failing. A fallback produces a reduced suite and the release is flagged DEGRADED to the approver. |
| `rollback` | Tests fail on every attempt. The workspace is restored to baseline and the run safe-stops. |
| `policy-block` | The implement agent writes a credential outside the approved scope. Guardrails block it (no retry) and roll back. |
| `replan` | The requirement changes twice mid-run. A wording-only change converges (no downstream rework). Adding expiry re-runs only the affected sub-graph. |

## Where each requirement is implemented

| Assignment requirement | Implementation | Proven by |
|---|---|---|
| Requirement understanding, ambiguity | `agents.normalize`: spec, ambiguities, assumptions, rejected items, acceptance criteria | `test_ambiguous_requirement_is_normalised_and_signed_off` |
| Task decomposition with dependencies | `agents.plan`: task DAG, AC mapping, parallel waves | report §2 |
| Codebase reasoning (brownfield) | `reasoning.py`: AST index of routes, functions, SQL, tables; feature detection; impact; data flows | `test_brownfield_reasons_about_existing_code`, `test_reasoning_detects_features_and_flows` |
| Explicit dependency graph, entry/exit gates | `engine.Pipeline` (validated DAG), `Stage.requires`, `Stage.exit_gate` | `test_rejects_cycles_and_unknown_deps`, `test_entry_gate_blocks_without_retry`, `test_exit_gate_failure_is_retried` |
| Sequential + parallel paths with synchronisation | Thread-pool batches with a join barrier | `test_parallel_stages_really_run_concurrently` |
| Cross-stage context and decision lineage | Content-hashed stage inputs/outputs, revisions, `audit.jsonl` | `test_audit_log_is_complete_and_ordered` |
| Human approval for high-impact actions | Conditional gates: ambiguity sign-off, schema migration, release. Pause, persist, resume. | `test_human_gate_pauses_persists_and_resumes_in_new_process`, `test_ambiguous_pauses_then_resumes`, `test_release_rejection_stops_without_release` |
| Bounded retries, fallback, rollback, safe-stop | `Orchestrator._run_stage`, `_rollback`, `_stop_reason` (STOP file, attempt and wall-clock budgets, re-plan oscillation cap) | `test_bounded_retry_*`, `test_fallback_*`, `test_exhausted_retries_roll_back_workspace`, `test_operator_stop_file_safe_stops`, `test_attempt_budget_safe_stops` |
| Policy guardrails: security, compliance, change control | `policy.py`: SEC-001..003, CMP-001, CHG-001..002, enforced at implement exit and in security review | `test_policy_*`, `test_detects_secrets_*`, `test_change_control_*` |
| Reliability metrics | `compute_metrics` / `aggregate_metrics`: success rate, retries, rollbacks, fallbacks, MTTR, e2e latency, approval wait | `test_aggregate_metrics`, `python -m orchestrator metrics` |
| Dynamic re-plan on upstream change | Hash-based freshness: only stages whose inputs changed re-run, and approvals bound to old inputs are invalidated | `test_replan_*` (engine + scenario) |
| Production-quality outputs | `app/` (promoted output of the `ambiguous` run), generated unit + API tests, `docs/API.md`, `CHANGELOG.md`, patch | `tests/test_store.py`, `tests/test_api.py`, `test_committed_app_is_the_promoted_ambiguous_output` |

## Layout

```
app/                    URL shortener (FastAPI + SQLite): the reviewed, promoted run output
orchestrator/
  engine.py             DAG scheduler, gates, retries, fallback, rollback, safe-stop, approvals, audit, metrics
  agents.py             role agents + pipeline wiring + approval hooks
  reasoning.py          AST codebase index and impact analysis
  policy.py             security / compliance / change-control rules
  codegen.py            template codegen with feature flags and AC -> test map
  templates/            source of truth for generated service code and tests
  scenarios.py          scenario catalogue and fault injection
  report.py             markdown + mermaid run report
tests/                  service tests (generated) + engine, policy and scenario tests
docs/ARCHITECTURE.md    components, control flow, key decisions
docs/scenarios/         committed sample reports for every scenario
ENGINEERING_SUMMARY.md  plan, rationale, risks, assumptions, limitations
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the orchestration model and
[ENGINEERING_SUMMARY.md](ENGINEERING_SUMMARY.md) for decisions, trade-offs and limitations.
