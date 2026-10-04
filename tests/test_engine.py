"""Engine unit tests on small synthetic graphs (fast, no codegen, no subprocess)."""

import json
import time

import pytest

from orchestrator.engine import (
    ChangeEvent,
    Orchestrator,
    Pipeline,
    PolicyViolation,
    RunState,
    Stage,
    StageFailure,
    aggregate_metrics,
    auto_approver,
    deny_approver,
    pause_approver,
)
from orchestrator.policy import Violation


def const(value):
    return lambda ctx: dict(value)


def orch(tmp_path, stages, approver=None, **kw):
    kw.setdefault("backoff_seconds", 0)
    return Orchestrator(Pipeline(stages), tmp_path / "runs", approver or auto_approver(), **kw)


def test_rejects_cycles_and_unknown_deps():
    with pytest.raises(ValueError, match="cycle"):
        Pipeline([Stage("a", const({}), ("b",)), Stage("b", const({}), ("a",))])
    with pytest.raises(ValueError, match="unknown"):
        Pipeline([Stage("a", const({}), ("ghost",))])


def test_levels_expose_parallel_paths():
    p = Pipeline([Stage("r", const({})), Stage("a", const({}), ("r",)), Stage("b", const({}), ("r",)), Stage("j", const({}), ("a", "b"))])
    assert p.levels() == [["r"], ["a", "b"], ["j"]]


def test_parallel_stages_really_run_concurrently(tmp_path):
    def slow(ctx):
        time.sleep(0.3)
        return {"done": ctx.stage}

    stages = [Stage("r", const({})), Stage("a", slow, ("r",)), Stage("b", slow, ("r",)), Stage("join", const({}), ("a", "b"))]
    state = orch(tmp_path, stages).start("t", "req")
    assert state.status == "succeeded"
    a, b = state.stages["a"], state.stages["b"]
    assert a["batch"] == b["batch"]
    assert a["started_at"] < b["ended_at"] and b["started_at"] < a["ended_at"]  # overlapping windows
    assert max(a["ended_at"], b["ended_at"]) - min(a["started_at"], b["started_at"]) < 0.55
    assert state.stages["join"]["started_at"] >= max(a["ended_at"], b["ended_at"])  # barrier
    assert state.metrics["max_parallelism"] == 2


def test_bounded_retry_recovers_and_records_mttr(tmp_path):
    calls = {"n": 0}

    def flaky(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise StageFailure("transient")
        return {"ok": True}

    state = orch(tmp_path, [Stage("r", flaky, max_attempts=3)]).start("t", "req")
    assert state.status == "succeeded"
    assert len(state.stages["r"]["attempts"]) == 2
    assert state.metrics["retries"] == 1
    assert state.metrics["mttr_ms"] is not None
    assert state.incidents[0]["resolution"] == "retry"


def test_exhausted_retries_roll_back_workspace(tmp_path):
    def write(ctx):
        (ctx.workspace / "generated.py").write_text("x = 1\n")
        return {"files": ["generated.py"]}

    def boom(ctx):
        raise StageFailure("always")

    def seed(ws):
        (ws / "existing.py").write_text("keep = True\n")

    stages = [Stage("impl", write, mutates_workspace=True), Stage("test", boom, ("impl",), max_attempts=2)]
    o = orch(tmp_path, stages)
    state = o.start("t", "req", seed=seed)
    ws = o.run_dir(state) / "workspace"
    assert state.status == "rolled_back"
    assert len(state.stages["test"]["attempts"]) == 2
    assert state.stages["impl"]["status"] == "rolled_back"
    assert not (ws / "generated.py").exists()
    assert (ws / "existing.py").read_text() == "keep = True\n"
    assert state.metrics["rollbacks"] == 1


def test_policy_violation_is_not_retried(tmp_path):
    def hook(stage, output, ctx):
        return [Violation("SEC-001", "security", "block", "x.py", "secret")] if output.get("leak") else []

    o = Orchestrator(Pipeline([Stage("impl", const({"leak": True}), max_attempts=3, mutates_workspace=True)]), tmp_path, auto_approver(), policy_hook=hook)
    state = o.start("t", "req")
    assert state.status == "rolled_back"
    assert len(state.stages["impl"]["attempts"]) == 1
    assert state.stages["impl"]["violations"][0]["rule"] == "SEC-001"
    assert state.metrics["policy_blocks"] == 1


def test_fallback_then_optional_skip(tmp_path):
    def boom(ctx):
        raise StageFailure("down")

    stages = [
        Stage("r", const({})),
        Stage("fb", boom, ("r",), fallback=const({"degraded": True})),
        Stage("opt", boom, ("r",), optional=True),
        Stage("end", const({}), ("fb", "opt")),
    ]
    state = orch(tmp_path, stages).start("t", "req")
    assert state.status == "succeeded"
    assert state.stages["fb"]["status"] == "fallback"
    assert state.stages["fb"]["output"] == {"degraded": True}
    assert state.stages["opt"]["status"] == "skipped"
    assert state.metrics["fallbacks"] == 1


def test_entry_gate_blocks_without_retry(tmp_path):
    stages = [Stage("r", const({"a": 1})), Stage("x", const({}), ("r",), requires=("r.missing",), max_attempts=3)]
    state = orch(tmp_path, stages).start("t", "req")
    assert state.status == "rolled_back"
    assert len(state.stages["x"]["attempts"]) == 1
    assert "entry gate" in state.stages["x"]["error"]


def test_exit_gate_failure_is_retried(tmp_path):
    calls = {"n": 0}

    def agent(ctx):
        calls["n"] += 1
        return {"good": calls["n"] > 1}

    stage = Stage("x", agent, exit_gate=lambda out, ctx: [] if out["good"] else ["not good"], max_attempts=2)
    state = orch(tmp_path, [stage]).start("t", "req")
    assert state.status == "succeeded"
    assert "exit gate" in state.stages["x"]["attempts"][0]["error"]


def test_human_gate_pauses_persists_and_resumes_in_new_process(tmp_path):
    stages = [Stage("build", const({})), Stage("release", const({"shipped": True}), ("build",), approval=lambda o: "high impact")]
    paused = orch(tmp_path, stages, pause_approver).start("t", "req")
    assert paused.status == "waiting_approval"
    assert "release" not in paused.stages
    on_disk = RunState.load(tmp_path / "runs" / paused.run_id / "state.json")
    assert on_disk.pending_approval["stage"] == "release"
    resumed = orch(tmp_path, stages, auto_approver("alice")).resume(paused.run_id)  # fresh orchestrator
    assert resumed.status == "succeeded"
    assert resumed.approvals[-1]["by"] == "alice"


def test_rejection_safe_stops(tmp_path):
    stages = [Stage("build", const({})), Stage("release", const({}), ("build",), approval=lambda o: "high impact")]
    state = orch(tmp_path, stages, deny_approver("bob")).start("t", "req")
    assert state.status == "rejected"
    assert "release" not in state.stages


def _replan_graph():
    def root(ctx):
        return {"text": ctx.requirement}

    def interpret(ctx):
        return {"expiry": "expire" in ctx.outputs["root"]["text"]}

    def build(ctx):
        return {"built_with_expiry": ctx.outputs["interpret"]["expiry"]}

    return [Stage("root", root), Stage("interpret", interpret, ("root",)), Stage("build", build, ("interpret",))]


def test_replan_converges_when_upstream_output_unchanged(tmp_path):
    change = ChangeEvent("build", "Build a shortener (reworded)", "wording")
    state = orch(tmp_path, _replan_graph()).start("t", "Build a shortener", changes=[change])
    assert state.status == "succeeded"
    assert state.stages["root"]["revision"] == 2
    assert state.stages["interpret"]["revision"] == 2
    assert state.stages["build"]["revision"] == 1  # not re-run
    assert any(e["event"] == "replan-converged" and e["stage"] == "interpret" for e in state.events)


def test_replan_reruns_affected_subgraph_when_output_changes(tmp_path):
    change = ChangeEvent("build", "Build a shortener; links expire", "compliance")
    state = orch(tmp_path, _replan_graph()).start("t", "Build a shortener", changes=[change])
    assert state.stages["build"]["revision"] == 2
    assert state.stages["build"]["output"] == {"built_with_expiry": True}
    assert state.requirement_version == 2


def test_replan_invalidates_prior_approval(tmp_path):
    seen = []

    def approver(req):
        seen.append(req.input_hash)
        return auto_approver()(req)

    stages = _replan_graph()[:2] + [Stage("build", const({}), ("interpret",), approval=lambda o: "gate")]
    stages.append(Stage("after", const({}), ("build",)))
    change = ChangeEvent("build", "now with expire", "change")
    orch(tmp_path, stages, approver).start("t", "plain", changes=[change])
    assert len(seen) == 2 and seen[0] != seen[1]


def test_operator_stop_file_safe_stops(tmp_path):
    def stopper(ctx):
        (ctx.run_dir / "STOP").write_text("maintenance window")
        return {}

    stages = [Stage("a", stopper), Stage("b", const({}), ("a",))]
    state = orch(tmp_path, stages).start("t", "req")
    assert state.status == "stopped"
    assert "maintenance window" in state.stop_reason
    assert "b" not in state.stages


def test_attempt_budget_safe_stops(tmp_path):
    stages = [Stage("a", const({})), Stage("b", const({}), ("a",))]
    state = orch(tmp_path, stages, max_total_attempts=1).start("t", "req")
    assert state.status == "stopped"
    assert "budget" in state.stop_reason


def test_audit_log_is_complete_and_ordered(tmp_path):
    o = orch(tmp_path, [Stage("a", const({})), Stage("b", const({}), ("a",))])
    state = o.start("t", "req")
    lines = [json.loads(x) for x in (o.run_dir(state) / "audit.jsonl").read_text().splitlines()]
    assert [e["seq"] for e in lines] == list(range(1, len(lines) + 1))
    ends = [e for e in lines if e["event"] == "stage-end"]
    assert {e["stage"] for e in ends} == {"a", "b"}
    assert all(e["input_hash"] and e["revision"] == 1 for e in ends)
    assert (o.run_dir(state) / "report.md").exists()


def test_aggregate_metrics(tmp_path):
    o = orch(tmp_path, [Stage("a", const({}))])
    o.start("t", "one")
    o.start("t", "two")
    agg = aggregate_metrics(tmp_path / "runs")
    assert agg["finished"] == 2 and agg["success_rate"] == 1.0
