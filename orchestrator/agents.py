"""Role agents for each SDLC stage, plus the pipeline wiring.

Agents are deterministic so the demo runs offline, without an API key and without flake. Each one
does real work: requirement normalisation, task-graph decomposition, AST-based impact analysis,
code generation into an isolated workspace, a real pytest run, a policy scan, generated docs and
a unified diff for release review. A model-backed agent can replace any of them as long as it
keeps the ``Agent`` signature: ``(StageContext) -> dict``.
"""

from __future__ import annotations

import difflib
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from orchestrator import codegen, policy, reasoning
from orchestrator.engine import Pipeline, Stage, StageContext, StageFailure

DAY = 24 * 3600

# --------------------------------------------------------------------------- requirements

_VAGUE = {
    "smarter": (
        "What does 'smarter' mean for links?",
        "Link lifecycle (expiry) plus per-link click analytics.",
        "ML ranking or fraud scoring has no data, labels or explainability story; rejected for a regulated context.",
    ),
    "safe": (
        "Safe against which threats?",
        "Scheme allow-list (http/https), no embedded credentials, URL length cap, expiring links by default.",
        "Limits phishing-link lifetime and abuse of the redirector without adding user-facing friction.",
    ),
    "secure": (
        "Secure against which threats?",
        "Same controls as 'safe': allow-list, no credentials in URLs, length cap, default expiry.",
        "Concrete, testable controls instead of an open-ended security goal.",
    ),
    "bank": (
        "Which regulatory constraints apply?",
        "No PII (IP, user agent) stored in analytics; every change auditable; human sign-off before release.",
        "Data minimisation is the cheapest compliance control; the orchestrator already gives audit + approvals.",
    ),
    "fast": ("How fast?", "p95 redirect latency under 50 ms on a single node.", "Prototype target; real SLO is a product decision."),
    "scalable": ("What scale?", "Single-node SQLite; horizontal scale is out of scope.", "Avoids premature infra; noted as a limitation."),
}


def normalize(ctx: StageContext) -> dict:
    text = ctx.requirement.lower()
    mode = "brownfield" if ctx.outputs["intake"]["has_existing_code"] else "greenfield"
    features = ["create", "redirect", "analytics"]
    assumptions = []
    if not any(w in text for w in ("click", "analytic", "stat", "count", "track", "smarter")):
        assumptions.append("Analytics not mentioned; included as an aggregate click counter (core shortener capability).")
    else:
        assumptions.append("'Analytics' means an aggregate click counter per link, not an event pipeline.")
    ambiguities = [
        {"term": term, "question": q, "interpretation": i, "rationale": r}
        for term, (q, i, r) in _VAGUE.items()
        if term in text
    ]
    regulated = any(a["term"] in {"safe", "secure", "bank"} for a in ambiguities)
    expiry = regulated or any(w in text for w in ("expir", "ttl", "time-limited"))
    if expiry:
        features.append("expiry")
    rejected = []
    if any(a["term"] == "smarter" for a in ambiguities):
        rejected = [
            {"item": "ML fraud scoring", "why": "no training data or labels; unexplainable blocking decisions"},
            {"item": "custom vanity aliases", "why": "brand-impersonation risk; needs ownership verification"},
            {"item": "per-user click analytics", "why": "requires PII; conflicts with data minimisation"},
        ]
    spec = {
        "features": features,
        "expiry": expiry,
        "default_ttl_seconds": 30 * DAY if regulated else None,
        "max_ttl_seconds": 365 * DAY,
        "max_url_length": 2048,
        "analytics": "click_count",
        "pii_in_analytics": False,
    }
    criteria = [
        {"id": "AC1", "text": "POST /links returns 201 and a short code for a valid http(s) URL"},
        {"id": "AC2", "text": "GET /{code} responds 302 to the original URL"},
        {"id": "AC3", "text": "Each redirect increments clicks visible at GET /links/{code}/stats; unknown codes 404"},
        {"id": "AC4", "text": "Unsafe URLs (non-http(s), credentials, oversized) are rejected with 422"},
        {"id": "AC8", "text": "Analytics store no PII"},
    ]
    if expiry:
        criteria.append({"id": "AC5", "text": "Links past their TTL stop redirecting (410); invalid TTLs rejected"})
    if spec["default_ttl_seconds"]:
        criteria.append({"id": "AC6", "text": "Links without a TTL expire after 30 days by default"})
    if mode == "brownfield" and expiry:
        criteria.append({"id": "AC7", "text": "Existing links survive the schema change and keep redirecting"})
    out_of_scope = [
        {"item": "authentication / multi-tenant ownership", "why": "not requested; needed before public launch"},
        {"item": "rate limiting", "why": "belongs at the gateway; listed as a limitation"},
    ]
    if regulated:
        assumptions.append("Default TTL of 30 days chosen for regulated context; product owner must confirm.")
    return {
        "mode": mode,
        "spec": spec,
        "ambiguities": ambiguities,
        "assumptions": assumptions,
        "rejected": rejected,
        "out_of_scope": out_of_scope,
        "acceptance_criteria": sorted(criteria, key=lambda c: c["id"]),
    }


def plan(ctx: StageContext) -> dict:
    """Decompose the normalised spec into a task DAG and compute parallelisable waves."""
    norm = ctx.outputs["normalize"]
    spec, mode = norm["spec"], norm["mode"]
    tasks = [
        {"id": "T1", "title": "API contract + schema", "stage": "design", "deps": [], "ac": ["AC1", "AC2", "AC3"]},
        {"id": "T2", "title": "URL validation policy", "stage": "implement_api", "deps": ["T1"], "ac": ["AC4"]},
        {"id": "T3", "title": "Persistence: links table + code allocation", "stage": "implement_api", "deps": ["T1"], "ac": ["AC1"]},
        {"id": "T4", "title": "POST /links", "stage": "implement_api", "deps": ["T2", "T3"], "ac": ["AC1", "AC4"]},
        {"id": "T5", "title": "GET /{code} redirect + atomic click count", "stage": "implement_api", "deps": ["T3"], "ac": ["AC2", "AC3"]},
        {"id": "T6", "title": "GET /links/{code}/stats (no PII)", "stage": "implement_api", "deps": ["T3"], "ac": ["AC3", "AC8"]},
    ]
    impl = ["T4", "T5", "T6"]
    if spec["expiry"]:
        deps = ["T3", "T5"]
        if mode == "brownfield":
            tasks.append({"id": "T7", "title": "Additive migration: links.expires_at", "stage": "implement_api", "deps": ["T3"], "ac": ["AC7"]})
            deps.append("T7")
        tasks.append({"id": "T8", "title": "TTL validation + expiry enforcement (410)", "stage": "implement_api", "deps": deps, "ac": ["AC5", "AC6"]})
        impl.append("T8")
    tasks += [
        {"id": "T9", "title": "Unit + API tests mapped to acceptance criteria", "stage": "implement_tests", "deps": ["T1"], "ac": [c["id"] for c in norm["acceptance_criteria"]]},
        {"id": "T10", "title": "Run test suite", "stage": "test", "deps": impl + ["T9"], "ac": []},
        {"id": "T11", "title": "Security / compliance / change-control review", "stage": "security_review", "deps": ["T10"], "ac": ["AC8"]},
        {"id": "T12", "title": "API docs + changelog", "stage": "docs", "deps": ["T10"], "ac": []},
        {"id": "T13", "title": "Release readiness + human sign-off", "stage": "release_readiness", "deps": ["T11", "T12"], "ac": []},
    ]
    waves = _waves(tasks)
    return {"tasks": tasks, "waves": waves, "critical_path_length": len(waves)}


def _waves(tasks: list[dict]) -> list[list[str]]:
    depth: dict[str, int] = {}
    remaining = {t["id"]: t for t in tasks}
    while remaining:
        progressed = False
        for tid, t in list(remaining.items()):
            if all(d in depth for d in t["deps"]):
                depth[tid] = 1 + max((depth[d] for d in t["deps"]), default=-1)
                del remaining[tid]
                progressed = True
        if not progressed:
            raise StageFailure(f"task graph has a cycle among {sorted(remaining)}")
    waves: list[list[str]] = [[] for _ in range(max(depth.values()) + 1)]
    for tid, d in depth.items():
        waves[d].append(tid)
    return waves


# --------------------------------------------------------------------------- design


def design(ctx: StageContext) -> dict:
    norm = ctx.outputs["normalize"]
    spec = norm["spec"]
    # Reason about what existed before this run (the baseline), not files this run already generated.
    baseline = ctx.run_dir / "baseline"
    index = reasoning.index_codebase(baseline)
    present = reasoning.detect_features(index)
    target = set(spec["features"])
    delta = sorted(target - present)
    removed = sorted(present - target)
    impact = reasoning.impact_analysis(index, set(delta)) if index["modules"] else []
    migrations = []
    if "expiry" in delta and "links" in index["tables"]:
        migrations.append(
            {"sql": "ALTER TABLE links ADD COLUMN expires_at REAL", "kind": "additive", "backward_compatible": True,
             "rollback": "previous build ignores the nullable column; no down-migration needed"}
        )
    risks = [
        {"risk": "Shortener is an open redirector by design", "mitigation": "scheme allow-list, no credentials, length cap; abuse reporting is out of scope"},
        {"risk": "Code enumeration", "mitigation": "7-char random codes from 62 symbols (~3.5e12 space) via secrets"},
        {"risk": "SQLite single writer", "mitigation": "atomic UPDATE for clicks; swap to Postgres for multi-node"},
    ]
    if removed:
        risks.append({"risk": f"Spec drops existing features {removed}", "mitigation": "blocked; requires explicit product approval"})
    if migrations:
        risks.append({"risk": "Schema change on live data", "mitigation": "additive nullable column, migration test (AC7), human approval gate"})
    allowed = list(codegen.APP_FILES + codegen.TEST_FILES + codegen.DOC_FILES)
    return {
        "mode": norm["mode"],
        "existing_features": sorted(present),
        "delta": delta,
        "removed": removed,
        "impact": impact,
        "data_flows": reasoning.data_flows(index),
        "migrations": migrations,
        "api_contract": api_contract(spec),
        "schema": schema_def(spec),
        "files": {"create": [f for f in allowed if not (baseline / f).exists()], "modify": [f for f in allowed if (baseline / f).exists()]},
        "allowed_files": allowed,
        "risks": risks,
    }


def api_contract(spec: dict) -> list[dict]:
    create_req = {"url": "string (1..%d, http/https)" % spec["max_url_length"]}
    create_res = {"code": "string", "short_url": "string", "url": "string"}
    stats_res = {"code": "string", "clicks": "int", "created_at": "epoch seconds"}
    follow = {"302": "Location: original URL", "404": "unknown code", "422": "malformed code"}
    if spec["expiry"]:
        create_req["ttl_seconds"] = "int, optional, 1..%d" % spec["max_ttl_seconds"]
        create_res["expires_at"] = "epoch seconds | null"
        stats_res.update(expires_at="epoch seconds | null", expired="bool")
        follow["410"] = "link expired"
    return [
        {"method": "POST", "path": "/links", "request": create_req, "responses": {"201": create_res, "422": "invalid url/ttl"}},
        {"method": "GET", "path": "/{code}", "responses": follow},
        {"method": "GET", "path": "/links/{code}/stats", "responses": {"200": stats_res, "404": "unknown code"}},
        {"method": "GET", "path": "/healthz", "responses": {"200": {"status": "ok"}}},
    ]


def schema_def(spec: dict) -> dict:
    cols = {"code": "TEXT PRIMARY KEY", "url": "TEXT NOT NULL", "clicks": "INTEGER NOT NULL DEFAULT 0", "created_at": "REAL NOT NULL"}
    if spec["expiry"]:
        cols["expires_at"] = "REAL NULL"
    return {"links": cols}


# --------------------------------------------------------------------------- implement / test


def implement_api(ctx: StageContext) -> dict:
    spec = ctx.outputs["normalize"]["spec"]
    files = codegen.render(spec, codegen.APP_FILES)
    changed = codegen.write_files(ctx.workspace, files)
    return {"files": changed, "generated": sorted(files)}


def implement_tests(ctx: StageContext) -> dict:
    spec = ctx.outputs["normalize"]["spec"]
    files = codegen.render(spec, codegen.TEST_FILES)
    changed = codegen.write_files(ctx.workspace, files)
    text = "\n".join(files.values())
    return {"files": changed, "generated": sorted(files), "test_functions": text.count("\ndef test_"), "degraded": False}


def implement_tests_fallback(ctx: StageContext) -> dict:
    """Degraded mode: keep store-level tests only. Release checklist flags the coverage gap."""
    spec = ctx.outputs["normalize"]["spec"]
    files = codegen.render(spec, codegen.FALLBACK_TEST_FILES)
    changed = codegen.write_files(ctx.workspace, files)
    return {"files": changed, "generated": sorted(files), "degraded": True, "missing": ["tests/test_api.py"]}


def compile_gate(output: dict, ctx: StageContext) -> list[str]:
    problems = []
    for rel in output.get("generated", []):
        path = ctx.workspace / rel
        try:
            compile(path.read_text(encoding="utf-8"), rel, "exec")
        except SyntaxError as exc:
            problems.append(f"{rel}: {exc.msg} (line {exc.lineno})")
    return problems


def run_tests(ctx: StageContext) -> dict:
    junit = ctx.run_dir / f"junit-{ctx.stage}-r{ctx.attempt}.xml"
    # Isolated config: the workspace run must never inherit the host repo's pytest settings.
    ini = ctx.run_dir / "pytest-isolated.ini"
    ini.write_text("[pytest]\n", encoding="utf-8")
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(ini),
           f"--rootdir={ctx.workspace}", f"--junitxml={junit}", "tests"]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "SHORTENER_DB": str(ctx.run_dir / "test-scratch.db")}
    try:
        proc = subprocess.run(cmd, cwd=ctx.workspace, capture_output=True, text=True, timeout=300, env=env)
    except subprocess.TimeoutExpired as exc:
        raise StageFailure("pytest timed out after 300s") from exc
    if not junit.exists():
        raise StageFailure(f"pytest produced no report (exit {proc.returncode}): {proc.stderr[-400:]}")
    root = ET.parse(junit).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    cases = []
    for tc in suite.iter("testcase"):
        status = "passed"
        if tc.find("failure") is not None or tc.find("error") is not None:
            status = "failed"
        elif tc.find("skipped") is not None:
            status = "skipped"
        cases.append({"name": tc.get("name", "").split("[")[0], "status": status})
    result = {
        "command": "python -m pytest -q",
        "exit_code": proc.returncode,
        "passed": sum(c["status"] == "passed" for c in cases),
        "failed": sum(c["status"] == "failed" for c in cases),
        "skipped": sum(c["status"] == "skipped" for c in cases),
        "cases": sorted({(c["name"], c["status"]) for c in cases}),
    }
    if proc.returncode != 0 or result["failed"]:
        raise StageFailure(f"{result['failed']} tests failed: {proc.stdout[-600:]}")
    if result["passed"] == 0:
        raise StageFailure("no tests ran")
    return result


def security_review(ctx: StageContext) -> dict:
    allowed = ctx.outputs["design"]["allowed_files"]
    # Code only: docs runs in parallel with this stage and is policy-checked by its own exit hook.
    changed = [f for f in changed_files(ctx.run_dir / "baseline", ctx.workspace) if f.startswith(("app/", "tests/"))]
    violations = policy.scan(ctx.workspace) + policy.scan(ctx.workspace, files=changed, allowed=allowed)
    blocking = [v for v in violations if v.severity == "block"]
    if blocking:
        from orchestrator.engine import PolicyViolation

        raise PolicyViolation(blocking)
    return {"rules": sorted(policy.RULES), "files_scanned": len(changed), "violations": [v.to_dict() for v in violations]}


def docs(ctx: StageContext) -> dict:
    d, norm = ctx.outputs["design"], ctx.outputs["normalize"]
    lines = ["# URL Shortener API", "", f"Generated from requirement: _{ctx.requirement}_", ""]
    for ep in d["api_contract"]:
        lines.append(f"## `{ep['method']} {ep['path']}`")
        if ep.get("request"):
            lines.append("")
            lines.append("Request body:")
            lines += [f"- `{k}`: {v}" for k, v in ep["request"].items()]
        lines.append("")
        lines.append("| Status | Body |")
        lines.append("|---|---|")
        for code, body in ep["responses"].items():
            lines.append(f"| {code} | {body if isinstance(body, str) else ', '.join(f'`{k}`' for k in body)} |")
        lines.append("")
    lines.append("## Schema")
    lines.append("")
    for table, cols in d["schema"].items():
        lines.append(f"`{table}`: " + ", ".join(f"{c} {t}" for c, t in cols.items()))
    lines.append("")
    lines.append("## Acceptance criteria")
    lines.append("")
    lines += [f"- **{c['id']}** {c['text']}" for c in norm["acceptance_criteria"]]
    changelog = ["# Changelog", "", "## Unreleased", ""]
    if d["delta"]:
        changelog += [f"- Added: {f}" for f in d["delta"]]
    changelog += [f"- Migration: `{m['sql']}` ({m['kind']})" for m in d["migrations"]]
    if not d["delta"]:
        changelog.append("- No functional change")
    files = {"docs/API.md": "\n".join(lines) + "\n", "CHANGELOG.md": "\n".join(changelog) + "\n"}
    return {"files": codegen.write_files(ctx.workspace, files), "generated": sorted(files)}


# --------------------------------------------------------------------------- release


def changed_files(baseline: Path, workspace: Path) -> list[str]:
    def listing(root: Path) -> dict[str, str]:
        return {
            p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
            for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts
        }

    a, b = listing(baseline), listing(workspace)
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def unified_diff(baseline: Path, workspace: Path, files: list[str]) -> str:
    chunks = []
    for rel in files:
        old = (baseline / rel).read_text(encoding="utf-8").splitlines(keepends=True) if (baseline / rel).exists() else []
        new = (workspace / rel).read_text(encoding="utf-8").splitlines(keepends=True) if (workspace / rel).exists() else []
        chunks.extend(difflib.unified_diff(old, new, f"a/{rel}", f"b/{rel}"))
    return "".join(chunks)


def release_readiness(ctx: StageContext) -> dict:
    o = ctx.outputs
    files = changed_files(ctx.run_dir / "baseline", ctx.workspace)
    passed_tests = {name for name, status in o["test"]["cases"] if status == "passed"}
    trace = {}
    for c in o["normalize"]["acceptance_criteria"]:
        proofs = [t for t in codegen.AC_TESTS.get(c["id"], ()) if t in passed_tests]
        trace[c["id"]] = proofs
    degraded = bool(o["implement_tests"].get("degraded"))
    checks = {
        "tests_green": o["test"]["failed"] == 0 and o["test"]["passed"] > 0,
        "acceptance_criteria_traced": all(trace.values()),
        "security_review_clean": not o["security_review"]["violations"],
        "change_within_design_scope": set(files) <= set(o["design"]["allowed_files"]),
        "docs_updated": "docs/API.md" in files or (ctx.workspace / "docs/API.md").exists(),
        "migrations_backward_compatible": all(m["backward_compatible"] for m in o["design"]["migrations"]),
        "full_test_suite": not degraded,
    }
    soft = {"full_test_suite", "acceptance_criteria_traced"} if degraded else {"full_test_suite"}
    hard = {k: v for k, v in checks.items() if k not in soft}
    release = ctx.run_dir / "release"
    release.mkdir(exist_ok=True)
    (release / "change.patch").write_text(unified_diff(ctx.run_dir / "baseline", ctx.workspace, files), encoding="utf-8")
    lines = ["# Release checklist", ""] + [f"- [{'x' if v else ' '}] {k}" for k, v in checks.items()]
    lines += ["", "## Acceptance criteria -> passing tests", ""] + [f"- {ac}: {', '.join(t) or 'NOT PROVEN'}" for ac, t in trace.items()]
    lines += ["", "## Rollback plan", "", "- Revert to the previous build; " + ("schema change is additive and nullable." if o["design"]["migrations"] else "no schema change.")]
    (release / "CHECKLIST.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"checks": checks, "blocking_ok": all(hard.values()), "traceability": trace, "changed_files": files,
            "patch": "release/change.patch", "degraded": degraded}


def release_gate(output: dict, ctx: StageContext) -> list[str]:
    # In degraded (fallback) mode, traceability gaps are expected and surfaced to the human
    # approver as warnings instead of blocking; everything else stays a hard gate.
    soft = {"full_test_suite", "acceptance_criteria_traced"} if output["degraded"] else {"full_test_suite"}
    return [k for k, v in output["checks"].items() if not v and k not in soft]


# --------------------------------------------------------------------------- approval hooks


def approve_assumptions(outputs: dict) -> str | None:
    amb = outputs.get("normalize", {}).get("ambiguities")
    if amb:
        return f"Requirement is ambiguous ({', '.join(a['term'] for a in amb)}); product owner must sign off on the interpretation before design."
    return None


def approve_migration(outputs: dict) -> str | None:
    migs = outputs.get("design", {}).get("migrations")
    if migs:
        return "Schema change on existing data: " + "; ".join(m["sql"] for m in migs)
    return None


def approve_release(outputs: dict) -> str | None:
    degraded = outputs.get("implement_tests", {}).get("degraded")
    return "Release is a high-impact action and needs human sign-off" + (" (DEGRADED: API tests missing)" if degraded else "")


# --------------------------------------------------------------------------- policy hook


def policy_hook(stage: str, output: dict, ctx: StageContext) -> list:
    if stage not in {"implement_api", "implement_tests", "docs"}:
        return []
    allowed = ctx.outputs.get("design", {}).get("allowed_files")
    return policy.scan(ctx.workspace, files=output.get("files", []), allowed=allowed)


# --------------------------------------------------------------------------- pipeline


def intake(ctx: StageContext) -> dict:
    return {"requirement": ctx.requirement.strip(), "scenario": ctx.scenario, "has_existing_code": (ctx.run_dir / "baseline" / "app").exists()}


def build_pipeline(overrides: dict | None = None) -> Pipeline:
    a = {
        "intake": intake,
        "normalize": normalize,
        "plan": plan,
        "design": design,
        "implement_api": implement_api,
        "implement_tests": implement_tests,
        "test": run_tests,
        "security_review": security_review,
        "docs": docs,
        "release_readiness": release_readiness,
    }
    a.update(overrides or {})
    return Pipeline(
        [
            Stage("intake", a["intake"], description="capture raw requirement"),
            Stage("normalize", a["normalize"], ("intake",), requires=("intake.requirement",), description="spec, ambiguities, acceptance criteria"),
            Stage("plan", a["plan"], ("normalize",), requires=("normalize.spec",), approval=approve_assumptions, description="task DAG + waves"),
            Stage("design", a["design"], ("plan",), requires=("normalize.spec",), description="impact analysis, contract, migrations"),
            Stage("implement_api", a["implement_api"], ("design",), requires=("design.allowed_files",), exit_gate=compile_gate,
                  approval=approve_migration, mutates_workspace=True, description="generate service code"),
            Stage("implement_tests", a["implement_tests"], ("design",), requires=("normalize.acceptance_criteria",), exit_gate=compile_gate,
                  mutates_workspace=True, fallback=implement_tests_fallback, description="generate tests"),
            Stage("test", a["test"], ("implement_api", "implement_tests"), description="run pytest in workspace"),
            Stage("security_review", a["security_review"], ("test",), description="policy scan of full change"),
            Stage("docs", a["docs"], ("test",), mutates_workspace=True, description="API docs + changelog"),
            Stage("release_readiness", a["release_readiness"], ("security_review", "docs"), exit_gate=release_gate,
                  approval=approve_release, description="checklist, traceability, patch"),
        ]
    )
