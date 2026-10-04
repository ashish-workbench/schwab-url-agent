"""End-to-end scenarios through the real pipeline (codegen, AST reasoning, nested pytest, policies)."""

from pathlib import Path

import pytest

from orchestrator import agents, codegen, scenarios
from orchestrator.agents import changed_files
from orchestrator.engine import auto_approver, deny_approver, pause_approver

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runs(tmp_path):
    return tmp_path / "runs"


def _run(name, runs, approver=None):
    return scenarios.run(name, runs, approver or auto_approver())


def test_greenfield_builds_tests_and_packages_release(runs):
    state = _run("greenfield", runs)
    assert state.status == "succeeded", state.stop_reason
    o = {n: r["output"] for n, r in state.stages.items()}
    assert o["normalize"]["mode"] == "greenfield"
    assert o["test"]["passed"] > 0 and o["test"]["failed"] == 0
    assert ["implement_api", "implement_tests"] in state.batches
    assert all(o["release_readiness"]["checks"].values())
    assert (runs / state.run_id / "release" / "change.patch").read_text().count("+++ b/app/") == 3


def test_brownfield_reasons_about_existing_code(runs):
    state = _run("brownfield", runs)
    assert state.status == "succeeded", state.stop_reason
    design = state.stages["design"]["output"]
    names = {i["name"] for i in design["impact"]}
    assert {"links", "create_link", "record_click", "GET /{code}", "POST /links"} <= names
    assert design["delta"] == ["expiry"]
    assert design["migrations"][0]["kind"] == "additive"
    assert any(a["stage"] == "implement_api" and "Schema change" in a["reason"] for a in state.approvals)
    assert state.stages["release_readiness"]["output"]["traceability"]["AC7"]


def test_ambiguous_requirement_is_normalised_and_signed_off(runs):
    state = _run("ambiguous", runs)
    assert state.status == "succeeded", state.stop_reason
    norm = state.stages["normalize"]["output"]
    assert {a["term"] for a in norm["ambiguities"]} == {"smarter", "safe", "bank"}
    assert norm["spec"]["default_ttl_seconds"] == 30 * 24 * 3600
    assert "ML fraud scoring" in {r["item"] for r in norm["rejected"]}
    assert state.approvals[0]["stage"] == "plan"  # sign-off happens before any design work


def test_ambiguous_pauses_then_resumes(runs):
    paused = _run("ambiguous", runs, pause_approver)
    assert paused.status == "waiting_approval"
    assert paused.pending_approval["stage"] == "plan"
    resumed = scenarios.resume(paused.run_id, runs, auto_approver("reviewer"))
    assert resumed.status == "succeeded"
    assert {a["by"] for a in resumed.approvals} == {"reviewer"}


def test_release_rejection_stops_without_release(runs):
    state = _run("greenfield", runs, deny_approver("cab"))
    assert state.status == "rejected"
    assert "release_readiness" not in state.stages


def test_flaky_test_stage_recovers(runs):
    state = _run("flaky-test", runs)
    assert state.status == "succeeded"
    assert len(state.stages["test"]["attempts"]) == 2
    assert state.metrics["mttr_ms"] is not None


def test_fallback_marks_release_degraded(runs):
    state = _run("fallback", runs)
    assert state.status == "succeeded"
    assert state.stages["implement_tests"]["status"] == "fallback"
    assert state.stages["release_readiness"]["output"]["degraded"] is True
    assert "DEGRADED" in state.approvals[-1]["reason"]


def test_rollback_restores_baseline(runs):
    state = _run("rollback", runs)
    assert state.status == "rolled_back"
    run_dir = runs / state.run_id
    assert changed_files(run_dir / "baseline", run_dir / "workspace") == []


def test_policy_block_stops_secret_and_scope_creep(runs):
    state = _run("policy-block", runs)
    assert state.status == "rolled_back"
    rec = state.stages["implement_api"]
    assert len(rec["attempts"]) == 1
    assert {v["rule"] for v in rec["violations"]} >= {"SEC-001", "CHG-001"}
    assert not (runs / state.run_id / "workspace" / "app" / "debug_settings.py").exists()


def test_replan_converges_then_reruns_affected_subgraph(runs):
    state = _run("replan", runs)
    assert state.status == "succeeded"
    assert state.requirement_version == 3
    converged = [e for e in state.events if e["event"] == "replan-converged"]
    assert converged and converged[0]["stage"] == "normalize"
    assert state.stages["design"]["revision"] == 2  # once originally, once for the real change only
    assert state.stages["normalize"]["output"]["spec"]["expiry"] is True
    assert "AC5" in state.stages["release_readiness"]["output"]["traceability"]
    assert state.stages["normalize"]["output"]["mode"] == "greenfield"  # judged on baseline, not own output
    assert state.stages["design"]["output"]["migrations"] == []


def test_committed_app_is_the_promoted_ambiguous_output():
    """app/ and its tests are the reviewed output of the 'ambiguous' scenario; keep them in sync."""
    from orchestrator.engine import StageContext

    outputs = {"intake": {"has_existing_code": True}}
    ctx = StageContext("x", "ambiguous", scenarios.AMBIGUOUS_REQ, "normalize", 1, outputs, ROOT / "__none__", ROOT, lambda *a: None)
    spec = agents.normalize(ctx)["spec"]
    for rel, content in codegen.render(spec, codegen.APP_FILES + codegen.TEST_FILES).items():
        assert (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n") == content, f"{rel} drifted from templates"
