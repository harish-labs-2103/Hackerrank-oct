"""Entry point: `python code/main.py <command>` (run from the repository root).

With no arguments it runs the full challenge audit and writes ./output.csv, matching the
starter repository's `python3 code/main.py` contract. See README.md for all commands.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from davestigator.cli import app  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv += ["audit", "--all"]
    app(prog_name="Davestigator")
