# Common rules (read first, every step)

AI-generated, unreviewed by a human.

Repo: `~/local/oaken-core`. Read `PLAN.md`. It holds the decisions. Do not reopen them.

Core is an async pipeline and a plugin loader. It has no nouns. No message. No person.

Source to lift: `~/local/oaken-mail` at commit `cf3b187`. Read it with
`git -C ~/local/oaken-mail show cf3b187:src/oaken/<path>`. Never read the working tree. It is dirty.
Lifted files keep their SPDX header. Rename `oaken.` to `oaken_core.`. Add no oaken dependency.

Run: `uv run --python 3.12 pytest -q`. Python 3.11 or later. The machine default is 3.10.

How to work:
- Smallest complete change. Name what you skipped.
- Short answers. One topic at a time. No big tables.
- Ask when unclear. Put questions in a block at the end. Numbered. One bold line each. Your default beside it.
- Test every new branch. Lifted code keeps its tests.
- Do not commit. Do not push. The owner confirms the trailer first:
  `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Never invent another.
- End with what you skipped and what you did not verify.
