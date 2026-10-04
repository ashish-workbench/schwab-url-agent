"""Scenario catalogue: the three required scenarios plus resilience demonstrations.

Fault injection wraps real agents, so recovery paths (retry, fallback, policy block, rollback,
re-plan, approval pause/resume) are exercised against the same pipeline the happy path uses.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from orchestrator import agents, codegen
from orchestrator.engine import Agent, ChangeEvent, Orchestrator, StageContext, StageFailure

V1_SPEC = {"features": ["create", "redirect", "analytics"], "expiry": False}


def seed_existing_service(workspace: Path) -> None:
    """Brownfield starting point: the v1 service (no expiry) with its tests."""
    codegen.write_files(workspace, codegen.render(V1_SPEC, codegen.APP_FILES + codegen.TEST_FILES))


def fail_first(agent: Agent, times: int, message: str = "injected transient failure") -> Agent:
    lock, calls = threading.Lock(), {"n": 0}

    def wrapped(ctx: StageContext) -> dict:
        with lock:
            calls["n"] += 1
            n = calls["n"]
        if n <= times:
            raise StageFailure(f"{message} (call {n})")
        return agent(ctx)

    return wrapped


def leak_secret(agent: Agent) -> Agent:
    """Simulates an agent that over-reaches: writes a debug module with a credential."""

    def wrapped(ctx: StageContext) -> dict:
        out = agent(ctx)
        (ctx.workspace / "app" / "debug_settings.py").write_text('API_KEY = "sk_live_51HxDemoDemoDemo"\n', encoding="utf-8")
        out["files"] = sorted(out["files"] + ["app/debug_settings.py"])
        return out

    return wrapped


@dataclass
class Scenario:
    name: str
    requirement: str
    summary: str
    seed: Optional[Callable[[Path], None]] = None
    overrides: Callable[[], dict] = dict
    changes: list = field(default_factory=list)


GREENFIELD_REQ = "Build a URL shortener service with APIs to create short links, redirect, and report click counts."
BROWNFIELD_REQ = (
    "Existing shortener: add link expiry. Links accept an optional TTL, expired links must stop redirecting, "
    "and existing links must keep working."
)
AMBIGUOUS_REQ = "Make our links smarter and safe for a bank."

SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in [
        Scenario("greenfield", GREENFIELD_REQ, "New service from an empty workspace."),
        Scenario("brownfield", BROWNFIELD_REQ, "Enhancement on the existing v1 service: impact analysis, additive migration, approval.", seed_existing_service),
        Scenario("ambiguous", AMBIGUOUS_REQ, "Vague ask normalised into testable controls; sign-off on the interpretation.", seed_existing_service),
        Scenario(
            "flaky-test",
            GREENFIELD_REQ,
            "Test stage fails once (transient) and recovers on bounded retry.",
            overrides=lambda: {"test": fail_first(agents.run_tests, 1, "flaky test runner")},
        ),
        Scenario(
            "fallback",
            GREENFIELD_REQ,
            "Test generation keeps failing; fallback produces a reduced suite and release is flagged DEGRADED.",
            overrides=lambda: {"implement_tests": fail_first(agents.implement_tests, 99, "test generator unavailable")},
        ),
        Scenario(
            "rollback",
            BROWNFIELD_REQ,
            "Tests fail on every attempt; workspace is rolled back to baseline and the run safe-stops.",
            seed_existing_service,
            overrides=lambda: {"test": fail_first(agents.run_tests, 99, "regression in redirect path")},
        ),
        Scenario(
            "policy-block",
            BROWNFIELD_REQ,
            "Implement agent writes a credential outside the approved scope; guardrail blocks without retry and rolls back.",
            seed_existing_service,
            overrides=lambda: {"implement_api": leak_secret(agents.implement_api)},
        ),
        Scenario(
            "replan",
            GREENFIELD_REQ,
            "Requirement changes twice mid-flight: a wording-only edit converges with no downstream rework, "
            "then a real change (add expiry) re-runs only the affected sub-graph.",
            changes=[
                ChangeEvent("design", "Build a URL shortener: create short links, redirect them, report click counts.", "stakeholder reworded the ask"),
                ChangeEvent("test", GREENFIELD_REQ + " Links must expire after an optional TTL.", "compliance added expiry"),
            ],
        ),
    ]
}

CORE = ("greenfield", "brownfield", "ambiguous")


def make_orchestrator(scenario: str, runs_dir: Path, approver, **kwargs) -> Orchestrator:
    sc = SCENARIOS[scenario]
    return Orchestrator(agents.build_pipeline(sc.overrides()), runs_dir, approver, policy_hook=agents.policy_hook, **kwargs)


def run(scenario: str, runs_dir: Path, approver, requirement: Optional[str] = None, **kwargs):
    sc = SCENARIOS[scenario]
    orch = make_orchestrator(scenario, runs_dir, approver, **kwargs)
    return orch.start(sc.name, requirement or sc.requirement, seed=sc.seed, changes=sc.changes, options={"custom_requirement": bool(requirement)})


def resume(run_id: str, runs_dir: Path, approver, **kwargs):
    from orchestrator.engine import RunState

    state = RunState.load(Path(runs_dir) / run_id / "state.json")
    return make_orchestrator(state.scenario, runs_dir, approver, **kwargs).resume(run_id)
