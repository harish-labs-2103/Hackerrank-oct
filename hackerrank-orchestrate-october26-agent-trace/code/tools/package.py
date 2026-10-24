"""Build code.zip at the repository root (local file only; nothing is uploaded).

Includes runnable source, prompts, config, README and evaluation reports. Excludes dataset/,
virtualenvs, caches, nested archives, .env files and anything that looks like a secret.

    python code/tools/package.py
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
REPO = CODE.parent
EXCLUDE_DIRS = {"cache", "__pycache__", ".pytest_cache", ".venv", "venv", "env", "node_modules", ".ruff_cache"}
EXCLUDE_SUFFIX = {".pyc", ".zip", ".tar", ".gz", ".7z"}
SECRET = re.compile(r"(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{12,}", re.I)
REQUIRED = ["main.py", "README.md", "requirements.txt", "evaluation/evaluation_report.md", "evaluation/usage_report.md"]


def main() -> int:
    missing = [r for r in REQUIRED if not (CODE / r).is_file()]
    if missing:
        print(f"missing required files: {missing}")
        return 1
    files = []
    for p in sorted(CODE.rglob("*")):
        rel = p.relative_to(CODE)
        if p.is_dir() or any(part in EXCLUDE_DIRS for part in rel.parts) or p.suffix in EXCLUDE_SUFFIX:
            continue
        if p.name.startswith(".env"):
            continue
        if p.suffix in {".py", ".md", ".yaml", ".yml", ".toml", ".txt", ".json"} and SECRET.search(p.read_text(errors="ignore")):
            print(f"refusing to package possible secret in {rel}")
            return 1
        files.append((p, rel))
    out = REPO / "code.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p, rel in files:
            z.write(p, rel)  # zip root = contents of code/ (evaluation/... at top level)
    print(f"wrote {out} ({len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
