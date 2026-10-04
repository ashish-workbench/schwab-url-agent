"""CLI: python -m orchestrator <command>."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from orchestrator import engine, scenarios

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS = ROOT / "runs"


def _approver(mode: str):
    if mode == "prompt" and not sys.stdin.isatty():
        mode = "pause"
    return {
        "auto": engine.auto_approver(),
        "prompt": engine.console_approver,
        "pause": engine.pause_approver,
        "deny": engine.deny_approver(),
    }[mode]


def _line(state) -> str:
    m = state.metrics
    return (
        f"{state.scenario:13} {state.status:17} retries={m['retries']} fallbacks={m['fallbacks']} "
        f"policy_blocks={m['policy_blocks']} rollbacks={m['rollbacks']} replans={m['replans']} "
        f"approvals={m['approvals']} parallel={m['max_parallelism']} mttr_ms={m['mttr_ms']} e2e_ms={m['e2e_latency_ms']}  "
        f"run={state.run_id}"
    )


def cmd_run(args) -> int:
    state = scenarios.run(args.scenario, Path(args.runs_dir), _approver(args.approve), requirement=args.requirement)
    print(_line(state))
    print(f"report: {Path(args.runs_dir) / state.run_id / 'report.md'}")
    if state.status == "waiting_approval":
        print(f"paused at {state.pending_approval['stage']}: {state.pending_approval['reason']}")
        print(f"resume with: python -m orchestrator resume {state.run_id} --approve prompt")
    return 0 if state.status in {"succeeded", "waiting_approval"} else 1


def cmd_resume(args) -> int:
    state = scenarios.resume(args.run_id, Path(args.runs_dir), _approver(args.approve))
    print(_line(state))
    return 0 if state.status == "succeeded" else 1


def cmd_stop(args) -> int:
    engine.Orchestrator.request_stop(Path(args.runs_dir), args.run_id, args.reason)
    print(f"stop requested for {args.run_id}; the run safe-stops before its next batch")
    return 0


def cmd_demo(args) -> int:
    runs = Path(args.runs_dir)
    names = list(scenarios.SCENARIOS)
    expected = {"rollback": "rolled_back", "policy-block": "rolled_back"}
    ok = True
    print("Running every scenario with demo auto-approval (recorded as approver=demo-auto).\n")
    results = []
    for name in names:
        state = scenarios.run(name, runs, engine.auto_approver())
        results.append(state)
        print(_line(state))
        ok &= state.status == expected.get(name, "succeeded")
    paused = scenarios.run("ambiguous", runs, engine.pause_approver)
    print(_line(paused) + "   <- pause at human gate")
    resumed = scenarios.resume(paused.run_id, runs, engine.auto_approver("reviewer@demo"))
    print(_line(resumed) + "   <- resumed after approval")
    ok &= paused.status == "waiting_approval" and resumed.status == "succeeded"
    print("\nAggregate reliability metrics:")
    print(json.dumps(engine.aggregate_metrics(runs), indent=2))
    if args.publish:
        out = Path(args.publish)
        out.mkdir(parents=True, exist_ok=True)
        for state in results:
            shutil.copy(runs / state.run_id / "report.md", out / f"{state.scenario}.md")
        print(f"\nreports copied to {out}")
    print("\nALL SCENARIOS BEHAVED AS EXPECTED" if ok else "\nUNEXPECTED SCENARIO OUTCOME")
    return 0 if ok else 1


def cmd_metrics(args) -> int:
    print(json.dumps(engine.aggregate_metrics(Path(args.runs_dir)), indent=2))
    return 0


def cmd_scenarios(args) -> int:
    for s in scenarios.SCENARIOS.values():
        print(f"{s.name:13} {s.summary}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m orchestrator", description="Agentic SDLC orchestrator")
    p.add_argument("--runs-dir", default=str(DEFAULT_RUNS))
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one scenario")
    r.add_argument("scenario", choices=sorted(scenarios.SCENARIOS))
    r.add_argument("--requirement", help="override the scenario's requirement text")
    r.add_argument("--approve", choices=["prompt", "auto", "pause", "deny"], default="prompt",
                   help="how human gates are answered (default: prompt; pause when not interactive)")
    r.set_defaults(fn=cmd_run)
    rs = sub.add_parser("resume", help="resume a paused run")
    rs.add_argument("run_id")
    rs.add_argument("--approve", choices=["prompt", "auto", "deny"], default="prompt")
    rs.set_defaults(fn=cmd_resume)
    st = sub.add_parser("stop", help="request safe-stop of a run")
    st.add_argument("run_id")
    st.add_argument("--reason", default="operator")
    st.set_defaults(fn=cmd_stop)
    d = sub.add_parser("demo", help="run every scenario and print metrics")
    d.add_argument("--publish", help="copy each report.md into this folder")
    d.set_defaults(fn=cmd_demo)
    sub.add_parser("metrics", help="aggregate metrics across runs").set_defaults(fn=cmd_metrics)
    sub.add_parser("scenarios", help="list scenarios").set_defaults(fn=cmd_scenarios)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
