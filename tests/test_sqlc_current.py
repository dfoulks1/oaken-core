# SPDX-License-Identifier: Apache-2.0
"""The generated query code (src/oaken_core/dbgen) matches what ``sqlc generate`` makes from the migrations and queries today."""

import filecmp
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SQLC = shutil.which("sqlc") or str(ROOT / ".venv" / "bin" / "sqlc")


@pytest.mark.skipif(not Path(SQLC).exists(), reason="sqlc is not installed (uv sync --group dev)")
def test_generated_code_is_current(tmp_path: Path) -> None:
    out = ROOT / f"sqlc_check_{tmp_path.name}"  # paths in a config are relative to it, so both sit in the repo root for the run
    cfg = (ROOT / "sqlc.yaml").read_text().replace("out: src/oaken_core/dbgen/sqlite", f"out: {out.name}")
    temp = ROOT / f"sqlc.check-{tmp_path.name}.yaml"
    temp.write_text(cfg)
    try:
        run = subprocess.run([SQLC, "generate", "-f", str(temp)], cwd=ROOT, capture_output=True, text=True, check=False)
        assert run.returncode == 0, run.stderr
        diff = filecmp.dircmp(out, ROOT / "src" / "oaken_core" / "dbgen" / "sqlite")
        assert not (diff.diff_files or diff.left_only or diff.right_only), "run: uv run sqlc generate"
    finally:
        temp.unlink()
        shutil.rmtree(out, ignore_errors=True)
