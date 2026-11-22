#!/usr/bin/env bash
# QA and docs build, in one go: ruff, mypy, pytest, then regenerate and check the site.
# Usage: tools/qa.sh [--no-mypy]   (mypy is reported, not gating, until its backlog is cleared)
set -u
cd "$(dirname "$0")/.."
fail=0
step() { echo "== $*"; }
step ruff;   uv run ruff check src tests docs-src/tools || fail=1
if [ "${1:-}" != "--no-mypy" ]; then step mypy; uv run mypy src/oaken_core || echo "(mypy not gating)"; fi
step pytest; uv run pytest -q || fail=1
step "docs: class map"; uv run --group docs python docs-src/tools/classmap.py --check || fail=1
step "docs: build";     uv run --group docs python docs-src/tools/build_site.py || fail=1
step "docs: check";     uv run --group docs python docs-src/tools/check_docs.py || fail=1
step "docs: tool tests"; uv run --group docs pytest -q docs-src/tools/tests || fail=1
[ $fail = 0 ] && echo "QA OK" || echo "QA FAILED"
exit $fail
