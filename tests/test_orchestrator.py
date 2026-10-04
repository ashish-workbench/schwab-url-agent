from orchestrator.engine import run_scenario
from orchestrator.scenarios import AGENTS, ambiguous, brownfield, greenfield


def test_greenfield_passes():
    state = greenfield()
    assert state.metrics["success"]
    assert state.stages["implement_api"].status == "passed"
    assert state.stages["implement_tests"].status == "passed"
    assert state.decisions


def test_brownfield_names_impact():
    state = brownfield()
    assert state.metrics["success"]
    assert "app/main.py:redirect" in state.stages["design"].output["impacted"]


def test_ambiguous_normalizes_and_refuses_ml():
    state = ambiguous()
    assert state.metrics["success"]
    assert state.context["spec"]["expiry"] is True
    assert "fraud model" in state.context["spec"]["rejected"]
    assert state.context["assumptions"]


def test_retry_then_rollback():
    state = run_scenario(
        "greenfield",
        "build a URL shortener with create, redirect, click count",
        AGENTS,
        fail_test_once=True,
    )
    # first test attempt fails, second passes (failure_armed only on attempt 1)
    assert state.stages["test"].status == "passed"
    assert state.stages["test"].attempts == 2
    assert state.metrics["retries"] >= 1


def test_human_gate_pauses():
    state = run_scenario(
        "greenfield",
        "build a URL shortener with create, redirect, click count",
        AGENTS,
        auto_approve=False,
    )
    assert state.stopped
    assert state.stages["release_readiness"].status == "waiting_approval"


def test_replan_marks_downstream_stale():
    from orchestrator.scenarios import replan

    state = replan()
    assert any(d["decision"] == "replan" for d in state.decisions)
    assert state.metrics["success"]
