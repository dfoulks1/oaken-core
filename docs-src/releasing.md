+++
title = "Releasing Oaken"
subtitle = "How oaken-core and oaken-plugins reach PyPI"
lead = "Oaken ships as two independent packages. This page is the whole procedure: what each package is, what to check, the order to upload in, and how to confirm a clean install works."
group = "project"
order = 10
description = "The release procedure for oaken-core and oaken-plugins: versions, the pin between them, the changelog, the build, the QA script, the upload order and the clean-install check."
+++

## The two packages

| Package | What it is | Repository |
|---|---|---|
| `oaken-core` | The pipeline, plugin host, operations, CLI, API, MCP door and console. It has no nouns. | `oaken-core` |
| `oaken-plugins` | The plugins you add to core: providers, formatters, triagers, enrichers, ingestors, backends. One distribution, a subpackage per plugin. | `oaken-plugins` |

They are separate projects with separate versions. `oaken-plugins` depends on core with a range
(`oaken-core>=1.0.0,<2`) and core never imports a plugin. A product (a mail client, a CRM) is a third
thing: a small package on top of both. It is not part of this release.

## Names on PyPI

- `oaken-core` is already on PyPI (0.1.0 and 0.2.0, the old oaken-mail bundle), so 1.0.0 is a new release of an existing
  project, uploaded with the token of the account that owns it. `oaken` and `oaken-cli` exist too (retired by 1.0.0).
- `oaken-plugins` was free when checked on 2026-11-22 (`/simple/oaken-plugins/` returned 404).
- Do not trust `/pypi/<name>/json` alone: it is 404 for a project that exists but has no files. Check `/simple/<name>/`
  (200 means the name is taken) before building a release around a name. The first upload registers the name to the
  account that holds the token.

## Versions and the pin

- Core and plugins version independently. Core follows [the stable surface](repo:docs-src/origin/0010-the-stable-surface-and-its-versioning.md):
  a breaking change to a plugin contract, an operation, a config key or a CLI command needs a new major.
- Raise the floor in `oaken-plugins` (`pyproject.toml`) only when it needs something core added. Say what
  in a comment beside the pin.
- Each plugin also declares the core versions it needs (`requires_core`). The host refuses one that does not fit.
- Unreleased work lives under `## Unreleased` in `CHANGELOG.md`. At release, rename the heading to the version.

## Before you build

1. Merge what is going out. A migration is numbered in order: if two branches both add `0009_*.sql`, the
   later merge renumbers its file and every test that lists migrations (`tests/test_store.py`, `tests/test_host.py`).
2. Fold the changelog and set `version` in `pyproject.toml`.
3. Run the QA script from the repository root:

   ```bash
   tools/QA.sh           # rebuild the docs, lint, a summary of what passed
   tools/QA.sh --tests   # the same, plus pytest
   ```

   It rebuilds the site into `docs/`, checks the class map, links and anchors, runs the docs tool tests,
   and runs ruff and mypy. Ruff gates; mypy and the formatter are reported only. If it changed `docs/`,
   commit that.
4. Review the changed documents and seal them (`bower` records that a human reviewed the exact bytes of a
   file, so seal last: any edit afterwards breaks the seal).

## Build and check

From each repository:

```bash
uv build                 # writes dist/<name>-<version>.tar.gz and .whl
uvx twine check dist/*   # both files must say PASSED
```

Look inside the source archive once: `tar tzf dist/<name>-<version>.tar.gz`. Core's is limited to `src`, `tests`,
`docs-src` and the project files by `[tool.hatch.build.targets.sdist]`, so local tooling and the generated site
do not ship.

## Prove a clean install

In a new virtual environment, with a throwaway home, install the two wheels and run the pipeline once:

```bash
python -m venv /tmp/v && /tmp/v/bin/pip install dist/*.whl
O="/tmp/v/bin/oaken --oaken-home /tmp/home"
$O start
$O plugins                       # the oaken-plugins catalog
$O plugin add formatter.ini --yes
$O intake submit --yes --source smoke --format ini --body $'[a]\nx=1\n[b]\ny=2'
$O items list                    # one item, with parts a and b
$O stop
```

Two things surprise a first-time user. `intake submit` prints the subject id of the submission, which is not the id
the stored item gets, so `oaken item show --id` with it finds nothing; use `oaken items list` (plural, the built-in
ingestor's command). `oaken item` (singular) is the core command group and has no `list`.

## Upload

Uploading to PyPI is final: a version number can never be used again, and a yanked release still counts as used.
Upload in this order, and stop between the two to confirm the first installs:

1. `oaken-core`, because `oaken-plugins` requires it.
2. `oaken-plugins`.

```bash
uv publish dist/oaken_core-<version>*       # needs a PyPI token (UV_PUBLISH_TOKEN)
uv publish dist/oaken_plugins-<version>*
```

Then tag each repository (`v<version>`), push the tag, and publish the GitHub release with the changelog entry.

## After release

- Reinstall both from PyPI in a new environment and repeat the clean-install check above.
- The old 0.x `oaken-core` bundle and the `oaken` and `oaken-cli` names were retired by 1.0.0. A final release of
  each that points here is a separate step. Check their status on PyPI first.

## AI-assisted work

Public-facing text (this site, release notes, announcements) that an AI helped write is reviewed by a human before it
ships, or is labelled AI-generated. See [CONTRIBUTING](repo:CONTRIBUTING.md). Each commit carries a `Co-Authored-By`
trailer naming the model that helped.
