import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.scenarios import ambiguous, brownfield, greenfield, replan


def main() -> None:
    for fn in (greenfield, brownfield, ambiguous, replan):
        state = fn()
        print(
            f"{state.scenario:12} success={state.metrics['success']} "
            f"retries={state.metrics['retries']} rollbacks={state.metrics['rollbacks']} "
            f"elapsed_ms={state.metrics['elapsed_ms']}"
        )
        for stage, result in state.stages.items():
            print(f"  {stage:20} {result.status} attempts={result.attempts}")


if __name__ == "__main__":
    main()
