# Run `20261004-085150-06afc5`: brownfield

**Status:** `succeeded`

**Requirement (v1):** Existing shortener: add link expiry. Links accept an optional TTL, expired links must stop redirecting, and existing links must keep working.

## Orchestration graph

```mermaid
flowchart LR
  intake["intake<br/>passed"]:::ok
  normalize["normalize<br/>passed"]:::ok
  plan["plan [approval hook]<br/>passed"]:::ok
  design["design<br/>passed"]:::ok
  implement_api["implement_api [approval hook]<br/>passed"]:::ok
  implement_tests["implement_tests<br/>passed"]:::ok
  test["test<br/>passed"]:::ok
  security_review["security_review<br/>passed"]:::ok
  docs["docs<br/>passed"]:::ok
  release_readiness["release_readiness [approval hook]<br/>passed"]:::ok
  intake --> normalize
  normalize --> plan
  plan --> design
  design --> implement_api
  design --> implement_tests
  implement_api --> test
  implement_tests --> test
  test --> security_review
  test --> docs
  security_review --> release_readiness
  docs --> release_readiness
  classDef ok fill:#d3f9d8,stroke:#2b8a3e
  classDef warn fill:#fff3bf,stroke:#e67700
  classDef bad fill:#ffe3e3,stroke:#c92a2a
  classDef idle fill:#f1f3f5,stroke:#868e96
```

## 1. Requirement understanding

Mode: **brownfield**. Features: create, redirect, analytics, expiry.

Assumptions:

- Analytics not mentioned; included as an aggregate click counter (core shortener capability).

Acceptance criteria:

- **AC1** POST /links returns 201 and a short code for a valid http(s) URL
- **AC2** GET /{code} responds 302 to the original URL
- **AC3** Each redirect increments clicks visible at GET /links/{code}/stats; unknown codes 404
- **AC4** Unsafe URLs (non-http(s), credentials, oversized) are rejected with 422
- **AC5** Links past their TTL stop redirecting (410); invalid TTLs rejected
- **AC7** Existing links survive the schema change and keep redirecting
- **AC8** Analytics store no PII

## 2. Task decomposition

| Task | Title | Stage | Depends on | Proves |
|---|---|---|---|---|
| T1 | API contract + schema | design | - | AC1, AC2, AC3 |
| T2 | URL validation policy | implement_api | T1 | AC4 |
| T3 | Persistence: links table + code allocation | implement_api | T1 | AC1 |
| T4 | POST /links | implement_api | T2, T3 | AC1, AC4 |
| T5 | GET /{code} redirect + atomic click count | implement_api | T3 | AC2, AC3 |
| T6 | GET /links/{code}/stats (no PII) | implement_api | T3 | AC3, AC8 |
| T7 | Additive migration: links.expires_at | implement_api | T3 | AC7 |
| T8 | TTL validation + expiry enforcement (410) | implement_api | T3, T5, T7 | AC5, AC6 |
| T9 | Unit + API tests mapped to acceptance criteria | implement_tests | T1 | AC1, AC2, AC3, AC4, AC5, AC7, AC8 |
| T10 | Run test suite | test | T4, T5, T6, T8, T9 | - |
| T11 | Security / compliance / change-control review | security_review | T10 | AC8 |
| T12 | API docs + changelog | docs | T10 | - |
| T13 | Release readiness + human sign-off | release_readiness | T11, T12 | - |

Parallel waves: {T1} -> {T2, T3, T9} -> {T4, T5, T6, T7} -> {T8} -> {T10} -> {T11, T12} -> {T13}

## 3. Design and codebase reasoning

Existing features: analytics, create, redirect. Delta: expiry.

| Kind | Symbol | File | Change |
|---|---|---|---|
| table | `links` | app/store.py | add nullable column expires_at |
| function | `create_link` | app/store.py:62 | accept ttl, persist expires_at |
| function | `record_click` | app/store.py:84 | refuse clicks after expiry (atomic predicate) |
| route | `POST /links` | app/main.py | accept optional ttl_seconds, return expires_at |
| route | `GET /links/{code}/stats` | app/main.py | expose expires_at / expired |
| route | `GET /{code}` | app/main.py | return 410 Gone for expired links |
| tests | `tests/test_api.py` | tests/test_api.py | extend coverage; 1 existing tests exercise impacted code |
| tests | `tests/test_store.py` | tests/test_store.py | extend coverage; 3 existing tests exercise impacted code |

Data flows found in the code:

- `POST /links -> create -> create_link -> links`
- `GET /links/{code}/stats -> stats -> get_link -> links`
- `GET /{code} -> follow -> record_click -> links`

Migrations:

- `ALTER TABLE links ADD COLUMN expires_at REAL` (additive; rollback: previous build ignores the nullable column; no down-migration needed)

Risks:

- Shortener is an open redirector by design: scheme allow-list, no credentials, length cap; abuse reporting is out of scope
- Code enumeration: 7-char random codes from 62 symbols (~3.5e12 space) via secrets
- SQLite single writer: atomic UPDATE for clicks; swap to Postgres for multi-node
- Schema change on live data: additive nullable column, migration test (AC7), human approval gate

## 4. Execution timeline

| Batch | Stage | Status | Attempts | Rev | Start (ms) | Duration (ms) |
|---|---|---|---|---|---|---|
| 0 | intake | passed | 1 | 1 | 2 | 0 |
| 1 | normalize | passed | 1 | 1 | 3 | 0 |
| 2 | plan | passed | 1 | 1 | 5 | 0 |
| 3 | design | passed | 1 | 1 | 7 | 6 |
| 4 | implement_api | passed | 1 | 1 | 15 | 10 |
| 4 | implement_tests | passed | 1 | 1 | 15 | 6 |
| 5 | test | passed | 1 | 1 | 27 | 887 |
| 6 | security_review | passed | 1 | 1 | 918 | 11 |
| 6 | docs | passed | 1 | 1 | 918 | 1 |
| 7 | release_readiness | passed | 1 | 1 | 932 | 2 |

Batches (stages in one batch ran concurrently and joined at a barrier): 0: intake; 1: normalize; 2: plan; 3: design; 4: implement_api, implement_tests; 5: test; 6: security_review, docs; 7: release_readiness

## 5. Validation

pytest: **29 passed, 0 failed** in the generated workspace.

Policy rules checked: CHG-001, CHG-002, CMP-001, SEC-001, SEC-002, SEC-003. Violations: 0.

Release checklist:

- [x] tests_green
- [x] acceptance_criteria_traced
- [x] security_review_clean
- [x] change_within_design_scope
- [x] docs_updated
- [x] migrations_backward_compatible
- [x] full_test_suite

Traceability (acceptance criterion -> passing tests):

- AC1: test_create_redirect_and_count, test_create_and_click
- AC2: test_create_redirect_and_count
- AC3: test_create_redirect_and_count, test_unknown_code_returns_404
- AC4: test_rejects_unsafe_urls, test_rejects_bad_urls, test_rejects_oversized_url
- AC5: test_expired_link_returns_410, test_expired_link_is_not_followed, test_rejects_invalid_ttl
- AC7: test_migration_keeps_existing_links
- AC8: test_stats_expose_no_pii

Changed files: CHANGELOG.md, app/main.py, app/store.py, docs/API.md, tests/test_api.py, tests/test_store.py. Reviewable patch: `release/change.patch`.

## 6. Governance

| Stage | Decision | By | Reason |
|---|---|---|---|
| implement_api | approved | demo-auto | Schema change on existing data: ALTER TABLE links ADD COLUMN expires_at REAL |
| release_readiness | approved | demo-auto | Release is a high-impact action and needs human sign-off |

## 7. Metrics

```json
{
  "success": true,
  "status": "succeeded",
  "stage_executions": 10,
  "attempts": 10,
  "failed_attempts": 0,
  "retries": 0,
  "first_pass_yield": 1.0,
  "rollbacks": 0,
  "fallbacks": 0,
  "policy_blocks": 0,
  "replans": 0,
  "approvals": 2,
  "incidents": 0,
  "mttr_ms": null,
  "e2e_latency_ms": 937,
  "active_stage_ms": 923,
  "approval_wait_ms": 0,
  "max_parallelism": 2
}
```

## 8. Decision log

| # | Stage | Event | Rev | Input hash | Detail |
|---|---|---|---|---|---|
| 1 | orchestrator | run-start |  |  | `{"scenario": "brownfield", "requirement": "Existing shortener: add link expiry. Links accept an optional TTL, expired links must stop redire...` |
| 2 | orchestrator | batch-start |  |  | `{"batch": 0, "stages": ["intake"], "parallel": false}` |
| 4 | intake | stage-end | 1 | 8c11003b4c7453cd | `{"status": "passed", "attempts": 1, "output_hash": "01256c8b0e2f83de"}` |
| 5 | orchestrator | batch-join |  |  | `{"batch": 0, "results": {"intake": "passed"}}` |
| 6 | orchestrator | batch-start |  |  | `{"batch": 1, "stages": ["normalize"], "parallel": false}` |
| 8 | normalize | stage-end | 1 | 7d8f8fa1e90df4ee | `{"status": "passed", "attempts": 1, "output_hash": "89581b6d803227f7"}` |
| 9 | orchestrator | batch-join |  |  | `{"batch": 1, "results": {"normalize": "passed"}}` |
| 10 | orchestrator | batch-start |  |  | `{"batch": 2, "stages": ["plan"], "parallel": false}` |
| 12 | plan | stage-end | 1 | 755d26e72bbfa9f0 | `{"status": "passed", "attempts": 1, "output_hash": "87df1049ce8ad9a4"}` |
| 13 | orchestrator | batch-join |  |  | `{"batch": 2, "results": {"plan": "passed"}}` |
| 14 | orchestrator | batch-start |  |  | `{"batch": 3, "stages": ["design"], "parallel": false}` |
| 16 | design | stage-end | 1 | 31f2aa7b53325b1e | `{"status": "passed", "attempts": 1, "output_hash": "fa2b89b11786fafa"}` |
| 17 | orchestrator | batch-join |  |  | `{"batch": 3, "results": {"design": "passed"}}` |
| 18 | implement_api | approval-requested |  |  | `{"reason": "Schema change on existing data: ALTER TABLE links ADD COLUMN expires_at REAL", "input_hash": "18ee0905dc823363"}` |
| 19 | implement_api | approved |  |  | `{"by": "demo-auto", "note": "auto-approved (demo mode, recorded for audit)"}` |
| 20 | orchestrator | batch-start |  |  | `{"batch": 4, "stages": ["implement_api", "implement_tests"], "parallel": true}` |
| 23 | implement_tests | stage-end | 1 | 18ee0905dc823363 | `{"status": "passed", "attempts": 1, "output_hash": "959b0aca11d0cf1e"}` |
| 24 | implement_api | stage-end | 1 | 18ee0905dc823363 | `{"status": "passed", "attempts": 1, "output_hash": "07c2fb7a8fb55d40"}` |
| 25 | orchestrator | batch-join |  |  | `{"batch": 4, "results": {"implement_tests": "passed", "implement_api": "passed"}}` |
| 26 | orchestrator | batch-start |  |  | `{"batch": 5, "stages": ["test"], "parallel": false}` |
| 28 | test | stage-end | 1 | 6690a29e77ea0a21 | `{"status": "passed", "attempts": 1, "output_hash": "c54bc014281bf92f"}` |
| 29 | orchestrator | batch-join |  |  | `{"batch": 5, "results": {"test": "passed"}}` |
| 30 | orchestrator | batch-start |  |  | `{"batch": 6, "stages": ["security_review", "docs"], "parallel": true}` |
| 33 | docs | stage-end | 1 | a2aed67a34cb1a18 | `{"status": "passed", "attempts": 1, "output_hash": "355923815ad135f9"}` |
| 34 | security_review | stage-end | 1 | a2aed67a34cb1a18 | `{"status": "passed", "attempts": 1, "output_hash": "4b2b209665a665a0"}` |
| 35 | orchestrator | batch-join |  |  | `{"batch": 6, "results": {"docs": "passed", "security_review": "passed"}}` |
| 36 | release_readiness | approval-requested |  |  | `{"reason": "Release is a high-impact action and needs human sign-off", "input_hash": "f384547b29708030"}` |
| 37 | release_readiness | approved |  |  | `{"by": "demo-auto", "note": "auto-approved (demo mode, recorded for audit)"}` |
| 38 | orchestrator | batch-start |  |  | `{"batch": 7, "stages": ["release_readiness"], "parallel": false}` |
| 40 | release_readiness | stage-end | 1 | f384547b29708030 | `{"status": "passed", "attempts": 1, "output_hash": "9523a4df9c6f28b3"}` |
| 41 | orchestrator | batch-join |  |  | `{"batch": 7, "results": {"release_readiness": "passed"}}` |
| 42 | orchestrator | run-complete |  |  | `{}` |
