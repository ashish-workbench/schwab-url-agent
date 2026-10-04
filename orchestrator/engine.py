"""Stateful, graph-based SDLC orchestrator.

Execution model
---------------
* The pipeline is an explicit DAG of ``Stage`` objects, validated (unknown deps, cycles) up front.
* Every loop iteration computes which stages are *fresh*. A stage is fresh when it finished OK,
  all its deps are fresh, and its recorded ``input_hash`` still matches the hash of its deps'
  current outputs. Anything not fresh is (re)scheduled. This one rule gives:
    - sequential and parallel paths: every ready stage in an iteration runs concurrently in a
      thread pool, and the iteration ends at a join barrier (synchronisation point);
    - dynamic re-planning: when an upstream output changes, downstream input hashes stop
      matching, so exactly the affected sub-graph re-runs; if an upstream stage re-runs and
      produces the same output, downstream stays fresh and is not re-run.
* Entry gate: deps fresh plus required upstream keys present. Exit gate: per-stage validator plus
  policy guardrails. Policy violations are not retried (deterministic) and trigger rollback.
* Bounded retries with backoff, then fallback (if defined), then skip (if optional), else rollback.
* Rollback restores the workspace to the run's baseline snapshot and safe-stops the run.
* Safe-stop: operator STOP file, attempt budget, wall-clock budget, per-stage revision cap.
* Human approval checkpoints: a stage's ``approval`` hook returns a reason when the action is
  high-impact. Approvals are bound to the stage's input hash, so a re-plan invalidates them.
  With no decision available the run pauses (``waiting_approval``), state is persisted, and
  ``resume`` continues it later, from another process if needed.
* Audit: every event is appended to ``audit.jsonl`` with sequence, stage revision and input hash
  (decision lineage). ``state.json`` is rewritten atomically after each batch.
"""

from __future__ import annotations

import concurrent.futures as cf
import copy
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from orchestrator.policy import Violation

OK = {"passed", "fallback", "skipped"}
TERMINAL = {"succeeded", "rolled_back", "stopped", "rejected"}
_IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")


# --------------------------------------------------------------------------- errors


class StageFailure(Exception):
    """A stage attempt failed. Retryable unless a subclass says otherwise."""

    retryable = True


class GateFailure(StageFailure):
    """Exit gate rejected the stage output."""


class EntryGateFailure(StageFailure):
    retryable = False


class PolicyViolation(StageFailure):
    retryable = False

    def __init__(self, violations: list[Violation]):
        self.violations = violations
        super().__init__("; ".join(f"{v.rule} {v.file}: {v.detail}" for v in violations))


# --------------------------------------------------------------------------- model


@dataclass
class StageContext:
    run_id: str
    scenario: str
    requirement: str
    stage: str
    attempt: int
    outputs: dict  # deep copy of every fresh upstream output, keyed by stage name
    workspace: Path
    run_dir: Path
    emit: Callable[[str, dict], None]


Agent = Callable[[StageContext], dict]
ExitGate = Callable[[dict, StageContext], list]
ApprovalHook = Callable[[dict], Optional[str]]
PolicyHook = Callable[[str, dict, StageContext], list]


@dataclass(frozen=True)
class Stage:
    name: str
    agent: Agent
    deps: tuple = ()
    requires: tuple = ()  # entry gate: "stage.key" that must exist upstream
    exit_gate: Optional[ExitGate] = None
    approval: Optional[ApprovalHook] = None
    mutates_workspace: bool = False
    optional: bool = False
    fallback: Optional[Agent] = None
    max_attempts: int = 2
    description: str = ""


class Pipeline:
    def __init__(self, stages: list[Stage]):
        self.stages = {s.name: s for s in stages}
        if len(self.stages) != len(stages):
            raise ValueError("duplicate stage names")
        for s in stages:
            missing = set(s.deps) - set(self.stages)
            if missing:
                raise ValueError(f"{s.name} depends on unknown stages {sorted(missing)}")
        self.order = self._toposort()

    def _toposort(self) -> list[str]:
        indeg = {n: len(s.deps) for n, s in self.stages.items()}
        children: dict[str, list[str]] = {n: [] for n in self.stages}
        for n, s in self.stages.items():
            for d in s.deps:
                children[d].append(n)
        queue = [n for n in self.stages if indeg[n] == 0]
        order = []
        while queue:
            n = queue.pop(0)
            order.append(n)
            for c in children[n]:
                indeg[c] -= 1
                if indeg[c] == 0:
                    queue.append(c)
        if len(order) != len(self.stages):
            raise ValueError("pipeline graph has a cycle")
        return order

    def levels(self) -> list[list[str]]:
        depth: dict[str, int] = {}
        for n in self.order:
            depth[n] = 1 + max((depth[d] for d in self.stages[n].deps), default=-1)
        out: list[list[str]] = [[] for _ in range(max(depth.values()) + 1)]
        for n in self.order:
            out[depth[n]].append(n)
        return out


@dataclass
class ApprovalRequest:
    run_id: str
    stage: str
    reason: str
    input_hash: str
    summary: dict


@dataclass
class ApprovalDecision:
    approved: bool
    by: str
    note: str = ""


Approver = Callable[[ApprovalRequest], Optional[ApprovalDecision]]


@dataclass
class ChangeEvent:
    """Upstream requirement change injected once ``after`` has passed (drives re-planning)."""

    after: str
    requirement: str
    reason: str = ""


@dataclass
class RunState:
    run_id: str
    scenario: str
    requirement: str
    options: dict
    created_at: float
    requirement_version: int = 1
    status: str = "running"
    stop_reason: str = ""
    stages: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    approvals: list = field(default_factory=list)
    pending_approval: Optional[dict] = None
    incidents: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    batches: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    ended_at: Optional[float] = None

    @classmethod
    def load(cls, path: Path) -> "RunState":
        return cls(**json.loads(path.read_text(encoding="utf-8")))


def stable_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- approvers


def auto_approver(by: str = "demo-auto") -> Approver:
    def _approve(req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(True, by, "auto-approved (demo mode, recorded for audit)")

    return _approve


def deny_approver(by: str = "reviewer") -> Approver:
    return lambda req: ApprovalDecision(False, by, "denied")


def pause_approver(req: ApprovalRequest) -> None:
    return None


def console_approver(req: ApprovalRequest) -> ApprovalDecision:
    import getpass

    print(f"\n[APPROVAL REQUIRED] run={req.run_id} stage={req.stage}\n  reason: {req.reason}")
    for k, v in req.summary.items():
        print(f"  {k}: {v}")
    answer = input("Approve? [y/N] ").strip().lower()
    return ApprovalDecision(answer in {"y", "yes"}, getpass.getuser(), "console")


# --------------------------------------------------------------------------- orchestrator


class Orchestrator:
    def __init__(
        self,
        pipeline: Pipeline,
        runs_dir: Path,
        approver: Approver,
        policy_hook: Optional[PolicyHook] = None,
        max_total_attempts: int = 40,
        max_wall_seconds: float = 900,
        max_stage_revisions: int = 5,
        backoff_seconds: float = 0.05,
    ):
        self.pipeline = pipeline
        self.runs_dir = Path(runs_dir)
        self.approver = approver
        self.policy_hook = policy_hook
        self.max_total_attempts = max_total_attempts
        self.max_wall_seconds = max_wall_seconds
        self.max_stage_revisions = max_stage_revisions
        self.backoff_seconds = backoff_seconds
        self._lock = threading.RLock()

    # ----- public API

    def start(
        self,
        scenario: str,
        requirement: str,
        seed: Optional[Callable[[Path], None]] = None,
        changes: Optional[list[ChangeEvent]] = None,
        options: Optional[dict] = None,
    ) -> RunState:
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        state = RunState(
            run_id=run_id,
            scenario=scenario,
            requirement=requirement,
            options=options or {},
            created_at=time.time(),
            changes=[{**asdict(c), "applied": False} for c in (changes or [])],
        )
        run_dir = self.run_dir(state)
        (run_dir / "workspace").mkdir(parents=True)
        if seed:
            seed(run_dir / "workspace")
        shutil.copytree(run_dir / "workspace", run_dir / "baseline", ignore=_IGNORE)
        self._log(state, "orchestrator", "run-start", {"scenario": scenario, "requirement": requirement, "graph": self.pipeline.levels()})
        return self._execute(state)

    def resume(self, run_id: str) -> RunState:
        state = RunState.load(self.runs_dir / run_id / "state.json")
        if state.status in TERMINAL:
            return state
        state.status = "running"
        self._log(state, "orchestrator", "resume", {"pending_approval": state.pending_approval})
        return self._execute(state)

    def run_dir(self, state: RunState) -> Path:
        return self.runs_dir / state.run_id

    @staticmethod
    def request_stop(runs_dir: Path, run_id: str, reason: str = "operator") -> None:
        (Path(runs_dir) / run_id / "STOP").write_text(reason, encoding="utf-8")

    # ----- main loop

    def _execute(self, state: RunState) -> RunState:
        self._segment_start = time.time()
        while True:
            reason = self._stop_reason(state)
            if reason:
                self._safe_stop(state, reason)
                break
            self._apply_changes(state)
            fresh = self._freshness(state)
            pending = [n for n in self.pipeline.order if not fresh[n]]
            if not pending:
                state.status = "succeeded"
                self._log(state, "orchestrator", "run-complete", {})
                break
            ready = [n for n in pending if all(fresh[d] for d in self.pipeline.stages[n].deps)]
            self._note_stale(state, ready)
            if not ready:
                self._safe_stop(state, "graph blocked: no runnable stage")
                break
            runnable, halted = self._approvals(state, ready)
            if halted:
                break
            if not self._run_batch(state, runnable):
                break
        return self._finish(state)

    def _stop_reason(self, state: RunState) -> str:
        stop_file = self.run_dir(state) / "STOP"
        if stop_file.exists():
            return "operator stop: " + stop_file.read_text(encoding="utf-8").strip()
        attempts = sum(len(r["attempts"]) for r in state.stages.values())
        if attempts >= self.max_total_attempts:
            return f"attempt budget exhausted ({attempts})"
        if time.time() - self._segment_start > self.max_wall_seconds:
            return f"wall-clock budget exhausted ({self.max_wall_seconds}s)"
        for name, rec in state.stages.items():
            if rec["revision"] > self.max_stage_revisions:
                return f"{name} re-planned more than {self.max_stage_revisions} times (oscillation guard)"
        return ""

    def _input_hash(self, state: RunState, name: str) -> str:
        stage = self.pipeline.stages[name]
        payload: dict = {"deps": {d: state.stages.get(d, {}).get("output_hash") for d in stage.deps}}
        if not stage.deps:
            payload["requirement"] = state.requirement
        return stable_hash(payload)

    def _freshness(self, state: RunState) -> dict[str, bool]:
        fresh: dict[str, bool] = {}
        for n in self.pipeline.order:
            rec = state.stages.get(n)
            fresh[n] = bool(
                rec
                and rec["status"] in OK
                and all(fresh[d] for d in self.pipeline.stages[n].deps)
                and rec["input_hash"] == self._input_hash(state, n)
            )
        return fresh

    def _note_stale(self, state: RunState, ready: list[str]) -> None:
        """A ready stage with an OK record is being invalidated: its inputs really changed."""
        for n in ready:
            rec = state.stages.get(n)
            if rec and rec["status"] in OK:
                self._log(
                    state,
                    n,
                    "replan-stale",
                    {"old_input": rec["input_hash"], "new_input": self._input_hash(state, n), "reason": "upstream output changed"},
                )

    def _apply_changes(self, state: RunState) -> None:
        for change in state.changes:
            rec = state.stages.get(change["after"])
            if change["applied"] or not rec or rec["status"] not in OK:
                continue
            change["applied"] = True
            old = state.requirement
            state.requirement = change["requirement"]
            state.requirement_version += 1
            self._log(
                state,
                "orchestrator",
                "requirement-changed",
                {"after": change["after"], "from": old, "to": state.requirement, "reason": change["reason"], "version": state.requirement_version},
            )

    # ----- approvals

    def _approvals(self, state: RunState, ready: list[str]) -> tuple[list[str], bool]:
        outputs = self._outputs(state)
        runnable = []
        for n in ready:
            hook = self.pipeline.stages[n].approval
            reason = hook(outputs) if hook else None
            if not reason:
                runnable.append(n)
                continue
            ih = self._input_hash(state, n)
            prior = [a for a in state.approvals if a["stage"] == n and a["input_hash"] == ih]
            if prior and prior[-1]["approved"]:
                runnable.append(n)
                continue
            req = ApprovalRequest(state.run_id, n, reason, ih, self._approval_summary(state, n))
            self._log(state, n, "approval-requested", {"reason": reason, "input_hash": ih})
            decision = self.approver(req)
            if decision is None:
                state.pending_approval = asdict(req)
                state.status = "waiting_approval"
                self._log(state, n, "paused-for-approval", {"reason": reason})
                self._persist(state)
                return [], True
            state.pending_approval = None
            state.approvals.append({"stage": n, "input_hash": ih, "approved": decision.approved, "by": decision.by, "note": decision.note, "ts": time.time(), "reason": reason})
            self._log(state, n, "approved" if decision.approved else "rejected", {"by": decision.by, "note": decision.note})
            if not decision.approved:
                state.status = "rejected"
                state.stop_reason = f"{n} rejected by {decision.by}"
                self._log(state, "orchestrator", "safe-stop", {"reason": state.stop_reason})
                return [], True
            runnable.append(n)
        return runnable, False

    def _approval_summary(self, state: RunState, stage: str) -> dict:
        out = self._outputs(state)
        summary = {"requirement": state.requirement}
        if "normalize" in out:
            summary["assumptions"] = out["normalize"].get("assumptions")
        if "design" in out:
            summary["migrations"] = out["design"].get("migrations")
        if "release_readiness" not in out and "test" in out:
            summary["tests"] = {k: out["test"].get(k) for k in ("passed", "failed")}
        return summary

    # ----- batch execution

    def _run_batch(self, state: RunState, names: list[str]) -> bool:
        batch_no = len(state.batches)
        state.batches.append(names)
        outputs = self._outputs(state)
        jobs = []
        for n in names:
            prev = state.stages.get(n)
            jobs.append((n, self._input_hash(state, n), (prev["revision"] if prev else 0) + 1))
        self._log(state, "orchestrator", "batch-start", {"batch": batch_no, "stages": names, "parallel": len(names) > 1})
        results: dict[str, dict] = {}
        with cf.ThreadPoolExecutor(max_workers=max(1, len(names)), thread_name_prefix="stage") as pool:
            futures = {
                pool.submit(self._run_stage, state, n, ih, rev, batch_no, copy.deepcopy(outputs)): n for n, ih, rev in jobs
            }
            for fut in cf.as_completed(futures):
                results[futures[fut]] = fut.result()
        self._log(state, "orchestrator", "batch-join", {"batch": batch_no, "results": {n: r["status"] for n, r in results.items()}})
        for n in names:  # deterministic merge order after the barrier
            prev = state.stages.get(n)
            state.stages[n] = results[n]
            if prev and prev["status"] in OK and prev["output_hash"] == results[n]["output_hash"]:
                kept = [c for c, s in self.pipeline.stages.items() if n in s.deps and c in state.stages]
                self._log(state, n, "replan-converged", {"output_hash": prev["output_hash"], "downstream_kept": kept})
        failed = [n for n in names if results[n]["status"] == "failed"]
        self._persist(state)
        if failed:
            self._rollback(state, failed)
            return False
        return True

    def _run_stage(self, state: RunState, name: str, input_hash: str, revision: int, batch: int, outputs: dict) -> dict:
        stage = self.pipeline.stages[name]
        run_dir = self.run_dir(state)
        rec: dict = {
            "status": "failed",
            "attempts": [],
            "output": None,
            "output_hash": None,
            "input_hash": input_hash,
            "revision": revision,
            "batch": batch,
            "started_at": time.time(),
            "ended_at": None,
            "error": None,
        }
        self._log(state, name, "stage-start", {"batch": batch}, revision=revision, input_hash=input_hash)

        def emit(event: str, detail: dict) -> None:
            self._log(state, name, event, detail, revision=revision, input_hash=input_hash)

        incident_open = False
        for attempt in range(1, stage.max_attempts + 1):
            ctx = StageContext(state.run_id, state.scenario, state.requirement, name, attempt, outputs, run_dir / "workspace", run_dir, emit)
            t0 = time.time()
            try:
                self._entry_gate(stage, outputs)
                output = stage.agent(ctx)
                problems = stage.exit_gate(output, ctx) if stage.exit_gate else []
                if problems:
                    raise GateFailure("exit gate: " + "; ".join(problems))
                if self.policy_hook:
                    violations = [v for v in self.policy_hook(name, output, ctx) if v.severity == "block"]
                    if violations:
                        raise PolicyViolation(violations)
                rec["attempts"].append({"n": attempt, "ok": True, "ms": int((time.time() - t0) * 1000)})
                rec.update(status="passed", output=output, output_hash=stable_hash(output))
                if attempt > 1:
                    emit("retry-recovered", {"attempt": attempt})
                    self._close_incident(state, name, "retry")
                break
            except StageFailure as exc:
                rec["attempts"].append({"n": attempt, "ok": False, "ms": int((time.time() - t0) * 1000), "error": str(exc)})
                rec["error"] = str(exc)
                if not incident_open:
                    self._open_incident(state, name, str(exc))
                    incident_open = True
                if isinstance(exc, PolicyViolation):
                    emit("policy-block", {"violations": [v.to_dict() for v in exc.violations]})
                    rec["violations"] = [v.to_dict() for v in exc.violations]
                    break
                emit("attempt-failed", {"attempt": attempt, "error": str(exc), "retryable": exc.retryable})
                if not exc.retryable:
                    break
                if attempt < stage.max_attempts:
                    time.sleep(self.backoff_seconds * (2 ** (attempt - 1)))
            except Exception as exc:  # unexpected agent crash: treated as retryable failure
                rec["attempts"].append({"n": attempt, "ok": False, "ms": int((time.time() - t0) * 1000), "error": repr(exc)})
                rec["error"] = repr(exc)
                if not incident_open:
                    self._open_incident(state, name, repr(exc))
                    incident_open = True
                emit("attempt-failed", {"attempt": attempt, "error": repr(exc), "retryable": True})
                if attempt < stage.max_attempts:
                    time.sleep(self.backoff_seconds * (2 ** (attempt - 1)))

        if rec["status"] != "passed" and not rec.get("violations"):
            if stage.fallback:
                ctx = StageContext(state.run_id, state.scenario, state.requirement, name, 0, outputs, run_dir / "workspace", run_dir, emit)
                try:
                    output = stage.fallback(ctx)
                    rec.update(status="fallback", output=output, output_hash=stable_hash(output))
                    emit("fallback", {"reason": rec["error"], "output": output})
                    self._close_incident(state, name, "fallback")
                except Exception as exc:  # fallback itself failed
                    emit("fallback-failed", {"error": repr(exc)})
            elif stage.optional:
                rec.update(status="skipped", output={"skipped": True}, output_hash=stable_hash({"skipped": True}))
                emit("skipped", {"reason": rec["error"]})
                self._close_incident(state, name, "skipped")
        rec["ended_at"] = time.time()
        emit("stage-end", {"status": rec["status"], "attempts": len(rec["attempts"]), "output_hash": rec["output_hash"]})
        return rec

    @staticmethod
    def _entry_gate(stage: Stage, outputs: dict) -> None:
        for req in stage.requires:
            dep, key = req.split(".", 1)
            if key not in (outputs.get(dep) or {}):
                raise EntryGateFailure(f"entry gate: missing {req}")

    # ----- rollback / stop

    def _rollback(self, state: RunState, failed: list[str]) -> None:
        run_dir = self.run_dir(state)
        self._log(state, "orchestrator", "rollback-start", {"failed": failed})
        workspace, baseline = run_dir / "workspace", run_dir / "baseline"
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.copytree(baseline, workspace, ignore=_IGNORE)
        reverted = []
        for n, rec in state.stages.items():
            if self.pipeline.stages[n].mutates_workspace and rec["status"] in OK:
                rec["status"] = "rolled_back"
                reverted.append(n)
        for n in failed:
            self._close_incident(state, n, "rollback")
        state.status = "rolled_back"
        state.stop_reason = f"{', '.join(failed)} failed; workspace restored to baseline"
        self._log(state, "orchestrator", "rollback-complete", {"reverted_stages": reverted, "workspace": "baseline"})
        self._log(state, "orchestrator", "safe-stop", {"reason": state.stop_reason})

    def _safe_stop(self, state: RunState, reason: str) -> None:
        state.status = "stopped"
        state.stop_reason = reason
        self._log(state, "orchestrator", "safe-stop", {"reason": reason})

    # ----- incidents, metrics, persistence

    def _open_incident(self, state: RunState, stage: str, error: str) -> None:
        with self._lock:
            state.incidents.append({"stage": stage, "opened_at": time.time(), "error": error, "closed_at": None, "resolution": None})

    def _close_incident(self, state: RunState, stage: str, resolution: str) -> None:
        with self._lock:
            for inc in reversed(state.incidents):
                if inc["stage"] == stage and inc["closed_at"] is None:
                    inc.update(closed_at=time.time(), resolution=resolution)
                    return

    def _finish(self, state: RunState) -> RunState:
        if state.status in TERMINAL:
            state.ended_at = time.time()
        state.metrics = compute_metrics(state)
        self._persist(state)
        try:
            from orchestrator.report import write_report

            write_report(state, self.pipeline, self.run_dir(state))
        except Exception as exc:  # reporting must never break a run
            self._log(state, "orchestrator", "report-failed", {"error": repr(exc)})
        return state

    def _outputs(self, state: RunState) -> dict:
        fresh = self._freshness(state)
        return {n: rec["output"] for n, rec in state.stages.items() if fresh.get(n)}

    def _log(self, state: RunState, stage: str, event: str, detail: dict, revision: Optional[int] = None, input_hash: Optional[str] = None) -> None:
        with self._lock:
            rec = state.stages.get(stage, {})
            entry = {
                "seq": len(state.events) + 1,
                "ts": time.time(),
                "run_id": state.run_id,
                "stage": stage,
                "event": event,
                "revision": revision if revision is not None else rec.get("revision"),
                "input_hash": input_hash if input_hash is not None else rec.get("input_hash"),
                "requirement_version": state.requirement_version,
                "detail": detail,
            }
            state.events.append(entry)
            path = self.run_dir(state) / "audit.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")

    def _persist(self, state: RunState) -> None:
        with self._lock:
            path = self.run_dir(state) / "state.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(state), indent=2, default=str), encoding="utf-8")
            os.replace(tmp, path)


def compute_metrics(state: RunState) -> dict:
    executions = list(state.stages.values())
    attempts = [a for r in executions for a in r["attempts"]]
    closed = [i for i in state.incidents if i["closed_at"]]
    end = state.ended_at or time.time()
    active_ms = sum(int(((r.get("ended_at") or r["started_at"]) - r["started_at"]) * 1000) for r in executions)
    approval_wait = 0.0
    for e in state.events:
        if e["event"] == "paused-for-approval":
            resumed = next((x["ts"] for x in state.events if x["seq"] > e["seq"] and x["event"] == "resume"), end)
            approval_wait += resumed - e["ts"]
    return {
        "success": state.status == "succeeded",
        "status": state.status,
        "stage_executions": sum(r["revision"] for r in executions),
        "attempts": len(attempts),
        "failed_attempts": sum(1 for a in attempts if not a["ok"]),
        "retries": sum(max(0, len(r["attempts"]) - 1) for r in executions),
        "first_pass_yield": round(sum(1 for r in executions if r["attempts"] and r["attempts"][0]["ok"]) / max(1, len(executions)), 3),
        "rollbacks": sum(1 for e in state.events if e["event"] == "rollback-complete"),
        "fallbacks": sum(1 for e in state.events if e["event"] == "fallback"),
        "policy_blocks": sum(1 for e in state.events if e["event"] == "policy-block"),
        "replans": sum(1 for e in state.events if e["event"] == "replan-stale"),
        "approvals": len(state.approvals),
        "incidents": len(state.incidents),
        "mttr_ms": int(sum((i["closed_at"] - i["opened_at"]) * 1000 for i in closed) / len(closed)) if closed else None,
        "e2e_latency_ms": int((end - state.created_at) * 1000),
        "active_stage_ms": active_ms,
        "approval_wait_ms": int(approval_wait * 1000),
        "max_parallelism": max((len(b) for b in state.batches), default=0),
        "stage_latency_ms": {
            n: int(((r.get("ended_at") or r["started_at"]) - r["started_at"]) * 1000) for n, r in state.stages.items()
        },
    }


def aggregate_metrics(runs_dir: Path) -> dict:
    states = []
    for path in sorted(Path(runs_dir).glob("*/state.json")):
        try:
            states.append(RunState.load(path))
        except Exception:
            continue
    finished = [s for s in states if s.status in TERMINAL]
    if not finished:
        return {"runs": len(states), "finished": 0}
    lat = sorted(s.metrics.get("e2e_latency_ms", 0) for s in finished)
    mttrs = [s.metrics["mttr_ms"] for s in finished if s.metrics.get("mttr_ms") is not None]

    def pct(p: float) -> int:
        return lat[min(len(lat) - 1, int(round(p * (len(lat) - 1))))]

    return {
        "runs": len(states),
        "finished": len(finished),
        "success_rate": round(sum(1 for s in finished if s.status == "succeeded") / len(finished), 3),
        "retry_frequency": round(sum(s.metrics.get("retries", 0) for s in finished) / len(finished), 3),
        "rollback_frequency": round(sum(s.metrics.get("rollbacks", 0) for s in finished) / len(finished), 3),
        "mttr_ms_mean": int(sum(mttrs) / len(mttrs)) if mttrs else None,
        "e2e_latency_ms_p50": pct(0.5),
        "e2e_latency_ms_p95": pct(0.95),
        "by_status": {k: sum(1 for s in finished if s.status == k) for k in sorted({s.status for s in finished})},
    }
