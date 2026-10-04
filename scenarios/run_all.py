"""Convenience entry point: equivalent to `python -m orchestrator demo`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["demo", *sys.argv[1:]]))
