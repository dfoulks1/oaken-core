# Contributing to Oaken Core

Pull requests are welcome. There is no DCO or CLA yet.

Core is an async pipeline and a plugin loader. It has no nouns: no message, no person. A change that
makes it know about one product (mail, a CRM) belongs in a plugin, not here.

## Development setup

You need Python 3.11 or newer and uv.

```bash
uv sync
uv run pytest
```

## Configuration

Oaken reads a single `config.toml`, outside the repo, in the per-user config directory:

| OS | Path |
|---|---|
| Linux | `~/.config/oaken/config.toml` |
| macOS | `~/Library/Application Support/oaken/config.toml` |
| Windows | `%APPDATA%\oaken\config.toml` |

Set `OAKEN_HOME=/some/dir` to relocate everything under one root (`<dir>/config/config.toml`). Use
that for development, so a test run never touches your real home.

**Secrets never go in `config.toml`.** `plugin add` refuses a secret setting.

## Repository layout

```text
src/oaken_core/   core: daemon, API, console, operations, jobs, queues, plugin host, store seam
tests/            core's tests
docs/             the website sources and generators (docs/tools)
docs/origin/      the architecture decisions core inherited from oaken-mail
prompts/          the build prompts, in order
```

Plugins live in their own repository (`oaken-plugins`) or in a product's. Core never imports
provider code: a plugin meets core at a declared contract.

## Ground rules

- Every Python file starts with `# SPDX-License-Identifier: Apache-2.0`.
- Tests never use the network, a model server or real data. Use synthetic fixtures.
- Never commit real content, addresses, databases or tokens.
- Use Conventional Commits for commit messages.
- A change that alters what an accepted ADR says is a new ADR or an amendment, never an edit of its text.
- AI-assisted contributions must follow the [AI tooling](#ai-tooling) rules below.

## AI tooling

Contributions produced with generative AI tooling must follow the ASF's
[Generative Tooling Guidance](https://www.apache.org/legal/generative-tooling.html). In short:

- You still represent that the contribution is your original creation and that you are entitled to
  license it under Apache-2.0 (ICLA §§4-5). Disclose any third-party material it contains.
- Make sure the tool's terms grant you rights broad enough to contribute the output under
  Apache-2.0, and don't restrict it in ways inconsistent with the Open Source Definition.
- Do not include GPL or otherwise Apache-incompatible third-party code. Where a tool can report
  similarity to its training data, or a code scanner is available, use it to gain reasonable
  certainty no copyrighted material was copied.
- Record the tool in the commit message with a provenance trailer, for example
  `Co-Authored-By: <Tool> <Version>` or `Generated-by: <Tool> <Version>`.
- Public-facing AI-generated text (the docs site, release notes, announcements, security
  advisories) must be reviewed by a committer before publishing, or be labeled as AI-generated
  (EU AI Act). Reviewing it as you would any other contribution is enough.

## Documentation

The website is generated locally from the Markdown in `docs/`, and the generated HTML is committed
beside it. GitHub Pages serves the `main` branch's `/docs` folder. After changing docs or the
software map, regenerate, check and commit the result:

```bash
uv run --group docs python docs/tools/build_site.py    # regenerate the HTML in docs/
uv run --group docs python docs/tools/check_docs.py    # links, anchors, and "HTML is up to date"
uv run python docs/tools/classmap.py                   # regenerate docs/softmap.classes.toml
```

The build records what it generated in `docs/.site-manifest` and only ever replaces those files.
Register a new page in `docs/site.toml`. Edit `docs/softmap.toml` in the same change that builds or
alters a component: the build fails if it disagrees with the repository. A test fails if
`docs/softmap.classes.toml` is stale.

Plugins document themselves (`plugin describe`), and the console builds the plugin docs from that.
Do not write them by hand.

## Security

Report a vulnerability privately; see [SECURITY.md](SECURITY.md). Participation is under the
[Code of Conduct](CODE_OF_CONDUCT.md).
