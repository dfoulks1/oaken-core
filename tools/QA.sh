#!/usr/bin/env bash
# QA for oaken-core: rebuild the docs, lint, and say what's what.
# Usage: tools/QA.sh [--tests]    (--tests also runs pytest; off by default)
# Runs every step, never stops early, exits 1 if any gating step failed.
# mypy is reported but does not gate until its backlog is cleared.
cd "$(dirname "$0")/.." || exit 2

names=(); results=(); notes=(); gating=()
run() {  # run LABEL GATING(1|0) COMMAND...
  local label=$1 gate=$2; shift 2
  echo; echo "== $label"
  local out; out=$("$@" 2>&1); local rc=$?
  echo "$out" | tail -n 15
  names+=("$label"); gating+=("$gate")
  if [ $rc = 0 ]; then results+=("ok"); else results+=("FAIL"); fi
  notes+=("$(echo "$out" | tail -n 1)")
}

run "docs: rebuild site"        1 uv run --group docs python docs-src/tools/build_site.py
run "docs: class map current"   1 uv run --group docs python docs-src/tools/classmap.py --check
run "docs: links and anchors"   1 uv run --group docs python docs-src/tools/check_docs.py
run "docs: tool tests"          1 uv run --group docs pytest -q docs-src/tools/tests
run "ruff: lint"                1 uv run ruff check src tests docs-src/tools
run "ruff: format (info)"       0 uv run ruff format --check src tests docs-src/tools
run "mypy (info)"               0 uv run mypy src/oaken_core
[ "${1:-}" = "--tests" ] && run "pytest" 1 uv run pytest -q

echo; echo "================ what's what ================"
bad=0
for i in "${!names[@]}"; do
  mark="${results[$i]}"
  [ "$mark" = FAIL ] && [ "${gating[$i]}" = 0 ] && mark="warn"
  [ "$mark" = FAIL ] && bad=1
  printf '%-6s %-26s %s\n' "$mark" "${names[$i]}" "${notes[$i]:0:70}"
done
echo
if [ -n "$(git status --porcelain docs)" ]; then echo "docs/ changed by the rebuild: review and commit it"; fi
[ $bad = 0 ] && echo "QA: nothing gating failed" || echo "QA: FAILED (see FAIL lines above)"
exit $bad
