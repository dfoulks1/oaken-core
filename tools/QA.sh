#!/usr/bin/env bash
# QA.sh: every check this repository asks for before a release, in one command. Run it from anywhere.
#   tools/QA.sh                      all checks, tests on Python 3.11 to 3.14
#   PYTHONS="3.12" tools/QA.sh       the same, with the tests on the Pythons you name (space separated)
#   tools/QA.sh --rebuild            first rebuild docs/ from docs-src/ (the only thing that writes to the tree)
# Needs uv. It runs every step and never stops early, then says what's what and exits 1 if anything that gates failed.
# What gates: the tests on each Python the project claims (3.11, 3.12, 3.13), the docs (rebuild is current, links and anchors, class map,
# the generator's own tests), the lock file and ruff. Reported but not gating: Pythons newer than the project claims, the ruff formatter
# and mypy (it has a backlog of findings; make it gate when that is cleared).
cd "$(dirname "$0")/.." || exit 2

REBUILD=0
[ "${1:-}" = "--rebuild" ] && REBUILD=1

CLAIMED="${PYTHONS:-3.11 3.12 3.13}"  # what pyproject.toml's classifiers promise
NEWER="${NEWER_PYTHONS-3.14}"         # tried too, as a warning only; NEWER_PYTHONS="" skips them

names=(); results=(); notes=(); gating=()
run() {  # run LABEL GATING(1|0) COMMAND...
  local label=$1 gate=$2; shift 2
  printf '\n== %s\n' "$label"
  local out rc
  out=$("$@" 2>&1); rc=$?
  printf '%s\n' "$out" | tail -n 15
  names+=("$label"); gating+=("$gate"); notes+=("$(printf '%s\n' "$out" | tail -n 1)")
  if [ $rc = 0 ]; then results+=("ok"); else results+=("FAIL"); fi
}

for v in $CLAIMED; do
  run "tests on python $v" 1 uv run --isolated --python "$v" pytest -q
done
for v in $NEWER; do
  run "tests on python $v (newer, info)" 0 uv run --isolated --python "$v" pytest -q
done

if [ "$REBUILD" = 1 ]; then
  run "docs: rebuild docs/ from docs-src/" 1 uv run --group docs python docs-src/tools/build_site.py
fi
run "docs: the class map is current"            1 uv run --group docs python docs-src/tools/classmap.py --check
run "docs: links, anchors, and docs/ is current" 1 uv run --group docs python docs-src/tools/check_docs.py
run "docs: generator tests"                     1 uv run --group docs pytest -q docs-src/tools/tests
run "uv.lock matches pyproject.toml"            1 uv lock --check
run "ruff: lint"                                1 uv run ruff check src tests docs-src/tools
run "ruff: format (info)"                       0 uv run ruff format --check src tests docs-src/tools
run "mypy (info)"                               0 uv run mypy src/oaken_core

printf '\n================ what is what ================\n'
bad=0
for i in "${!names[@]}"; do
  mark="${results[$i]}"
  [ "$mark" = FAIL ] && [ "${gating[$i]}" = 0 ] && mark="warn"
  [ "$mark" = FAIL ] && bad=1
  printf '%-5s %-44s %s\n' "$mark" "${names[$i]}" "${notes[$i]:0:60}"
done
printf '\n'
if [ -n "$(git status --porcelain docs 2>/dev/null)" ]; then
  echo "docs/ differs from the last commit: review and commit it (after sealing the pages a person reviewed)"
fi
version="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -n 1)"
if [ $bad = 0 ]; then printf 'QA ok (%s)\n' "$version"; else printf 'QA FAILED (%s): see the FAIL lines above\n' "$version"; fi
exit $bad
