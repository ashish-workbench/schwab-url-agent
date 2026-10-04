"""Deterministic SDLC orchestrator. Agents are role functions, not live model calls."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

MAX_ATTEMPTS = 2

GRAPH = {
    "intake": [],
    "normalize": ["intake"],
    "design": ["normalize"],
    "implement_api": ["design"],
    "implement_tests": ["design"],
    "test": ["implement_api", "implement_tests"],
    "docs": ["test"],
    "release_readiness": ["docs"],
}

PARALLEL = {"implement_api", "implement_tests"}
HUMAN_GATES = {"release_readiness"}
OPTIONAL = {"implement_tests"}


@dataclass
class StageResult:
    stage: str
    status: str
    attempts: int
    output: dict
    notes: list[str] = field(default_factory=list)


@dataclass
class RunState:
    run_id: str
    scenario: str
    requirement: str
    context: dict
    decisions: list[dict] = field(default_factory=list)
    stages: dict[str, StageResult] = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    stopped: bool = False
    rolled_back: bool = False

    def log(self, stage: str, decision: str, why: str) -> None:
        self.decisions.append(
            {"ts": time.time(), "stage": stage, "decision": decision, "why": why}
        )


Agent = Callable[[RunState], dict]


def _ready(state: RunState, stage: str) -> bool:
    return all(state.stages.get(dep) and state.stages[dep].status == "passed" for dep in GRAPH[stage])


def _stale_downstream(state: RunState, stage: str) -> None:
    victims = [name for name, deps in GRAPH.items() if stage in deps or _depends_on(name, stage)]
    for name in victims:
        state.stages.pop(name, None)
    state.log(stage, "replan", f"downstream of {stage} marked stale")


def _depends_on(name: str, ancestor: str, seen: set | None = None) -> bool:
    seen = seen or set()
    if name in seen:
        return False
    seen.add(name)
    for dep in GRAPH[name]:
        if dep == ancestor or _depends_on(dep, ancestor, seen):
            return True
    return False


def run_scenario(
    scenario: str,
    requirement: str,
    agents: dict[str, Agent],
    auto_approve: bool = True,
    inject_after_normalize: dict | None = None,
    fail_test_once: bool = False,
) -> RunState:
    started = time.time()
    state = RunState(
        run_id=str(uuid.uuid4())[:8],
        scenario=scenario,
        requirement=requirement,
        context={"requirement": requirement, "artifacts": [], "approvals": []},
        metrics={"retries": 0, "rollbacks": 0, "success": False},
    )
    failure_armed = fail_test_once
    pending = list(GRAPH)
    while pending and not state.stopped:
        ready = [name for name in pending if _ready(state, name)]
        if not ready:
            state.stopped = True
            state.log("orchestrator", "safe-stop", "no runnable stage; graph blocked")
            break
        # Parallel fan-out: run every ready implement_* together, else one stage.
        batch = [n for n in ready if n in PARALLEL] or ready[:1]
        for stage in batch:
            pending.remove(stage)
            _run_stage(state, stage, agents[stage], auto_approve, failure_armed)
            if stage == "test":
                failure_armed = False
            if stage == "normalize" and inject_after_normalize:
                state.context.update(inject_after_normalize)
                state.log("normalize", "upstream-change", str(inject_after_normalize))
                _stale_downstream(state, "normalize")
                for name in GRAPH:
                    if name not in state.stages and name not in pending:
                        pending.append(name)
        if state.rolled_back:
            break
    state.metrics["elapsed_ms"] = int((time.time() - started) * 1000)
    state.metrics["success"] = (
        not state.stopped
        and state.stages.get("release_readiness")
        and state.stages["release_readiness"].status == "passed"
    )
    recovered = [d for d in state.decisions if d["decision"] in {"retry-pass", "rollback"}]
    state.metrics["mttr_ms"] = state.metrics["elapsed_ms"] if recovered else 0
    return state


def _run_stage(state: RunState, stage: str, agent: Agent, auto_approve: bool, failure_armed: bool) -> None:
    if stage in HUMAN_GATES:
        state.log(stage, "human-gate", "release requires approval")
        if not auto_approve:
            state.stopped = True
            state.stages[stage] = StageResult(stage, "waiting_approval", 0, {})
            state.log(stage, "safe-stop", "paused for human approval")
            return
        state.context["approvals"].append({"stage": stage, "by": "demo-auto", "ts": time.time()})
        state.log(stage, "approved", "demo auto-approve")

    notes = []
    output: dict = {}
    status = "failed"
    attempts = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts = attempt
        try:
            if stage == "test" and failure_armed and attempt == 1:
                raise RuntimeError("injected test failure")
            output = agent(state)
            _policy(stage, output)
            status = "passed"
            if attempt > 1:
                state.log(stage, "retry-pass", f"recovered on attempt {attempt}")
            break
        except Exception as exc:  # bounded retry, then fallback or rollback
            notes.append(str(exc))
            state.metrics["retries"] += 1
            state.log(stage, "retry", f"attempt {attempt} failed: {exc}")
            if attempt == MAX_ATTEMPTS:
                if stage in OPTIONAL:
                    status = "skipped"
                    output = {"fallback": True}
                    state.log(stage, "fallback", "optional stage skipped after retries")
                else:
                    status = "rolled_back"
                    state.rolled_back = True
                    state.metrics["rollbacks"] += 1
                    state.context["artifacts"] = []
                    state.log(stage, "rollback", "reverted artifact set and safe-stopped")
    state.stages[stage] = StageResult(stage, status, attempts, output, notes)
    if status == "passed":
        state.context.setdefault("stage_outputs", {})[stage] = output


def _policy(stage: str, output: dict) -> None:
    blob = str(output).lower()
    if "api_key" in blob or "secret=" in blob:
        raise RuntimeError("policy: secret in artifact")
    if stage == "implement_api" and output.get("open_redirect"):
        raise RuntimeError("policy: unbounded redirect")
    if output.get("pii"):
        raise RuntimeError("policy: pii in analytics")
