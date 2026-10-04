"""Human-readable run report (markdown + mermaid) generated from state and audit log."""

from __future__ import annotations

import json
from pathlib import Path

_STATUS_CLASS = {"passed": "ok", "fallback": "warn", "skipped": "warn", "failed": "bad", "rolled_back": "bad"}


def _mermaid(state, pipeline) -> list[str]:
    lines = ["```mermaid", "flowchart LR"]
    for name in pipeline.order:
        rec = state.stages.get(name)
        status = rec["status"] if rec else "not run"
        gate = " [approval hook]" if pipeline.stages[name].approval else ""
        lines.append(f'  {name}["{name}{gate}<br/>{status}"]:::{_STATUS_CLASS.get(status, "idle")}')
    for name in pipeline.order:
        for dep in pipeline.stages[name].deps:
            lines.append(f"  {dep} --> {name}")
    lines += [
        "  classDef ok fill:#d3f9d8,stroke:#2b8a3e",
        "  classDef warn fill:#fff3bf,stroke:#e67700",
        "  classDef bad fill:#ffe3e3,stroke:#c92a2a",
        "  classDef idle fill:#f1f3f5,stroke:#868e96",
        "```",
    ]
    return lines


def render(state, pipeline) -> str:
    out = state.stages
    o = {n: r["output"] for n, r in out.items() if r.get("output")}
    m = state.metrics
    L = [f"# Run `{state.run_id}`: {state.scenario}", ""]
    L += [f"**Status:** `{state.status}`" + (f" ({state.stop_reason})" if state.stop_reason else ""), ""]
    L += [f"**Requirement (v{state.requirement_version}):** {state.requirement}", ""]
    L += ["## Orchestration graph", ""] + _mermaid(state, pipeline) + [""]

    if "normalize" in o:
        n = o["normalize"]
        L += ["## 1. Requirement understanding", "", f"Mode: **{n['mode']}**. Features: {', '.join(n['spec']['features'])}.", ""]
        if n["ambiguities"]:
            L += ["| Ambiguous term | Question | Interpretation | Rationale |", "|---|---|---|---|"]
            L += [f"| {a['term']} | {a['question']} | {a['interpretation']} | {a['rationale']} |" for a in n["ambiguities"]]
            L.append("")
        L += ["Assumptions:", ""] + [f"- {a}" for a in n["assumptions"]] + [""]
        if n["rejected"]:
            L += ["Rejected:", ""] + [f"- ~~{r['item']}~~: {r['why']}" for r in n["rejected"]] + [""]
        L += ["Acceptance criteria:", ""] + [f"- **{c['id']}** {c['text']}" for c in n["acceptance_criteria"]] + [""]

    if "plan" in o:
        p = o["plan"]
        L += ["## 2. Task decomposition", "", "| Task | Title | Stage | Depends on | Proves |", "|---|---|---|---|---|"]
        L += [f"| {t['id']} | {t['title']} | {t['stage']} | {', '.join(t['deps']) or '-'} | {', '.join(t['ac']) or '-'} |" for t in p["tasks"]]
        L += ["", "Parallel waves: " + " -> ".join("{" + ", ".join(w) + "}" for w in p["waves"]), ""]

    if "design" in o:
        d = o["design"]
        L += ["## 3. Design and codebase reasoning", "", f"Existing features: {', '.join(d['existing_features']) or 'none (greenfield)'}. Delta: {', '.join(d['delta']) or 'none'}.", ""]
        if d["impact"]:
            L += ["| Kind | Symbol | File | Change |", "|---|---|---|---|"]
            L += [f"| {i['kind']} | `{i['name']}` | {i['file']}{':' + str(i['line']) if i.get('line') else ''} | {i['change']} |" for i in d["impact"]]
            L.append("")
        if d["data_flows"]:
            L += ["Data flows found in the code:", ""] + [f"- `{f}`" for f in d["data_flows"]] + [""]
        if d["migrations"]:
            L += ["Migrations:", ""] + [f"- `{x['sql']}` ({x['kind']}; rollback: {x['rollback']})" for x in d["migrations"]] + [""]
        L += ["Risks:", ""] + [f"- {r['risk']}: {r['mitigation']}" for r in d["risks"]] + [""]

    L += ["## 4. Execution timeline", "", "| Batch | Stage | Status | Attempts | Rev | Start (ms) | Duration (ms) |", "|---|---|---|---|---|---|---|"]
    t0 = state.created_at
    for name, r in sorted(out.items(), key=lambda kv: kv[1]["started_at"]):
        dur = int(((r.get("ended_at") or r["started_at"]) - r["started_at"]) * 1000)
        L.append(f"| {r['batch']} | {name} | {r['status']} | {len(r['attempts'])} | {r['revision']} | {int((r['started_at'] - t0) * 1000)} | {dur} |")
    L += ["", "Batches (stages in one batch ran concurrently and joined at a barrier): " + "; ".join(f"{i}: {', '.join(b)}" for i, b in enumerate(state.batches)), ""]

    if "test" in o:
        t = o["test"]
        L += ["## 5. Validation", "", f"pytest: **{t['passed']} passed, {t['failed']} failed** in the generated workspace.", ""]
    if "security_review" in o:
        s = o["security_review"]
        L += [f"Policy rules checked: {', '.join(s['rules'])}. Violations: {len(s['violations'])}.", ""]
    if "release_readiness" in o:
        r = o["release_readiness"]
        L += ["Release checklist:", ""] + [f"- [{'x' if v else ' '}] {k}" for k, v in r["checks"].items()] + [""]
        L += ["Traceability (acceptance criterion -> passing tests):", ""] + [f"- {k}: {', '.join(v) or 'NOT PROVEN'}" for k, v in r["traceability"].items()] + [""]
        L += [f"Changed files: {', '.join(r['changed_files'])}. Reviewable patch: `{r['patch']}`.", ""]

    L += ["## 6. Governance", ""]
    if state.approvals:
        L += ["| Stage | Decision | By | Reason |", "|---|---|---|---|"]
        L += [f"| {a['stage']} | {'approved' if a['approved'] else 'rejected'} | {a['by']} | {a['reason']} |" for a in state.approvals]
        L.append("")
    if state.pending_approval:
        L += [f"**Waiting for approval** at `{state.pending_approval['stage']}`: {state.pending_approval['reason']}", ""]
    for name, r in out.items():
        for v in r.get("violations", []):
            L.append(f"- Policy block `{v['rule']}` ({v['category']}) in `{v['file']}`: {v['detail']}")
    if state.incidents:
        L += ["", "| Incident stage | Error | Resolution | Recovery (ms) |", "|---|---|---|---|"]
        for i in state.incidents:
            rec = int((i["closed_at"] - i["opened_at"]) * 1000) if i["closed_at"] else "-"
            L.append(f"| {i['stage']} | {i['error'][:80]} | {i['resolution'] or 'open'} | {rec} |")
        L.append("")

    L += ["## 7. Metrics", "", "```json", json.dumps({k: v for k, v in m.items() if k != "stage_latency_ms"}, indent=2), "```", ""]
    L += ["## 8. Decision log", "", "| # | Stage | Event | Rev | Input hash | Detail |", "|---|---|---|---|---|---|"]
    for e in state.events:
        if e["event"] in {"stage-start"}:
            continue
        detail = json.dumps(e["detail"], default=str)
        detail = (detail[:140] + "...") if len(detail) > 140 else detail
        L.append(f"| {e['seq']} | {e['stage']} | {e['event']} | {e['revision'] or ''} | {e['input_hash'] or ''} | `{detail.replace('|', '/')}` |")
    L.append("")
    return "\n".join(L)


def write_report(state, pipeline, run_dir: Path) -> Path:
    path = Path(run_dir) / "report.md"
    path.write_text(render(state, pipeline), encoding="utf-8")
    return path
