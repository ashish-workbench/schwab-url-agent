"""Scenario agents. Brownfield names impacted modules before editing."""

from orchestrator.engine import RunState, run_scenario


def _intake(state: RunState) -> dict:
    return {"raw": state.requirement, "source": state.scenario}


def _normalize(state: RunState) -> dict:
    text = state.requirement.lower()
    assumptions = []
    spec = {"create": True, "redirect": True, "clicks": True, "expiry": False, "schemes": ["http", "https"]}
    if "expir" in text:
        spec["expiry"] = True
    if "bank" in text or "smarter" in text or "safe" in text:
        assumptions.append("ambiguous ask normalized: block non-http(s), default ttl 30d, no PII in stats, no fraud ML")
        spec["expiry"] = True
        spec["default_ttl_seconds"] = 30 * 24 * 3600
        spec["rejected"] = ["fraud model", "custom alias", "user analytics"]
    state.context["spec"] = spec
    state.context["assumptions"] = assumptions
    return {"spec": spec, "assumptions": assumptions}


def _design(state: RunState) -> dict:
    spec = state.context["spec"]
    modules = ["app/store.py", "app/main.py", "tests/test_shortener.py"]
    if state.scenario == "brownfield":
        impacted = ["app/store.py:expires_at", "app/main.py:redirect", "tests/test_shortener.py:expiry"]
        state.log("design", "impact", ",".join(impacted))
        return {"modules": modules, "impacted": impacted, "change_control": "additive column, redirect gate"}
    return {"modules": modules, "impacted": modules if spec.get("expiry") else ["app/store.py", "app/main.py"]}


def _implement_api(state: RunState) -> dict:
    spec = state.context["spec"]
    artifact = {
        "endpoints": ["POST /links", "GET /{code}", "GET /links/{code}/stats"],
        "open_redirect": False,
        "pii": False,
        "expiry": spec.get("expiry", False),
    }
    state.context["artifacts"].append("api")
    return artifact


def _implement_tests(state: RunState) -> dict:
    state.context["artifacts"].append("tests")
    return {"cases": ["create", "redirect", "stats", "expiry" if state.context["spec"].get("expiry") else "no-expiry"]}


def _test(state: RunState) -> dict:
    if "api" not in state.context["artifacts"]:
        raise RuntimeError("api artifact missing")
    return {"passed": True}


def _docs(state: RunState) -> dict:
    return {"docs": ["README.md", "ENGINEERING_SUMMARY.md"], "assumptions": state.context.get("assumptions", [])}


def _release(state: RunState) -> dict:
    return {"ready": True, "approvals": state.context.get("approvals", [])}


AGENTS = {
    "intake": _intake,
    "normalize": _normalize,
    "design": _design,
    "implement_api": _implement_api,
    "implement_tests": _implement_tests,
    "test": _test,
    "docs": _docs,
    "release_readiness": _release,
}


def greenfield() -> RunState:
    return run_scenario("greenfield", "build a URL shortener with create, redirect, click count", AGENTS)


def brownfield() -> RunState:
    return run_scenario("brownfield", "existing shortener: add expiry and reject expired redirects", AGENTS)


def ambiguous() -> RunState:
    return run_scenario("ambiguous", "make links smarter and safe for a bank", AGENTS)


def replan() -> RunState:
    return run_scenario(
        "greenfield",
        "build a URL shortener with create, redirect, click count",
        AGENTS,
        inject_after_normalize={"constraint": "expiry required", "spec_override": True},
    )


def rollback_demo() -> RunState:
    return run_scenario(
        "greenfield",
        "build a URL shortener with create, redirect, click count",
        AGENTS,
        fail_test_once=False,
    )
