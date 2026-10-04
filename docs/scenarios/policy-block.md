# Run `20261004-085153-decae6`: policy-block

**Status:** `rolled_back` (implement_api failed; workspace restored to baseline)

**Requirement (v1):** Existing shortener: add link expiry. Links accept an optional TTL, expired links must stop redirecting, and existing links must keep working.

## Orchestration graph

```mermaid
flowchart LR
  intake["intake<br/>passed"]:::ok
  normalize["normalize<br/>passed"]:::ok
  plan["plan [approval hook]<br/>passed"]:::ok
  design["design<br/>passed"]:::ok
  implement_api["implement_api [approval hook]<br/>failed"]:::bad
  implement_tests["implement_tests<br/>rolled_back"]:::bad
  test["test<br/>not run"]:::idle
  security_review["security_review<br/>not run"]:::idle
  docs["docs<br/>not run"]:::idle
  release_readiness["release_readiness [approval hook]<br/>not run"]:::idle
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
| 2 | plan | passed | 1 | 1 | 4 | 0 |
| 3 | design | passed | 1 | 1 | 6 | 4 |
| 4 | implement_api | failed | 1 | 1 | 12 | 10 |
| 4 | implement_tests | rolled_back | 1 | 1 | 13 | 10 |

Batches (stages in one batch ran concurrently and joined at a barrier): 0: intake; 1: normalize; 2: plan; 3: design; 4: implement_api, implement_tests

## 6. Governance

| Stage | Decision | By | Reason |
|---|---|---|---|
| implement_api | approved | demo-auto | Schema change on existing data: ALTER TABLE links ADD COLUMN expires_at REAL |

- Policy block `SEC-001` (security) in `app/debug_settings.py`: matches (?i)\b(api[_-]?key|secret|password|token
- Policy block `CHG-001` (change-control) in `app/debug_settings.py`: file outside the approved design scope

| Incident stage | Error | Resolution | Recovery (ms) |
|---|---|---|---|
| implement_api | SEC-001 app/debug_settings.py: matches (?i)\b(api[_-]?key|secret|password|token; | rollback | 6 |

## 7. Metrics

```json
{
  "success": false,
  "status": "rolled_back",
  "stage_executions": 6,
  "attempts": 6,
  "failed_attempts": 1,
  "retries": 0,
  "first_pass_yield": 0.833,
  "rollbacks": 1,
  "fallbacks": 0,
  "policy_blocks": 1,
  "replans": 0,
  "approvals": 1,
  "incidents": 1,
  "mttr_ms": 6,
  "e2e_latency_ms": 27,
  "active_stage_ms": 24,
  "approval_wait_ms": 0,
  "max_parallelism": 2
}
```

## 8. Decision log

| # | Stage | Event | Rev | Input hash | Detail |
|---|---|---|---|---|---|
| 1 | orchestrator | run-start |  |  | `{"scenario": "policy-block", "requirement": "Existing shortener: add link expiry. Links accept an optional TTL, expired links must stop redi...` |
| 2 | orchestrator | batch-start |  |  | `{"batch": 0, "stages": ["intake"], "parallel": false}` |
| 4 | intake | stage-end | 1 | 8c11003b4c7453cd | `{"status": "passed", "attempts": 1, "output_hash": "7bc7b3eb9b55eda4"}` |
| 5 | orchestrator | batch-join |  |  | `{"batch": 0, "results": {"intake": "passed"}}` |
| 6 | orchestrator | batch-start |  |  | `{"batch": 1, "stages": ["normalize"], "parallel": false}` |
| 8 | normalize | stage-end | 1 | e3ce2b09a5e640b8 | `{"status": "passed", "attempts": 1, "output_hash": "89581b6d803227f7"}` |
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
| 23 | implement_api | policy-block | 1 | 18ee0905dc823363 | `{"violations": [{"rule": "SEC-001", "category": "security", "severity": "block", "file": "app/debug_settings.py", "detail": "matches (?i)\\b...` |
| 24 | implement_api | stage-end | 1 | 18ee0905dc823363 | `{"status": "failed", "attempts": 1, "output_hash": null}` |
| 25 | implement_tests | stage-end | 1 | 18ee0905dc823363 | `{"status": "passed", "attempts": 1, "output_hash": "959b0aca11d0cf1e"}` |
| 26 | orchestrator | batch-join |  |  | `{"batch": 4, "results": {"implement_api": "failed", "implement_tests": "passed"}}` |
| 27 | orchestrator | rollback-start |  |  | `{"failed": ["implement_api"]}` |
| 28 | orchestrator | rollback-complete |  |  | `{"reverted_stages": ["implement_tests"], "workspace": "baseline"}` |
| 29 | orchestrator | safe-stop |  |  | `{"reason": "implement_api failed; workspace restored to baseline"}` |
