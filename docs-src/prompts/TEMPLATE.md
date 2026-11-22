# Title: the job, as a short sentence

<!-- Copy this file to prompts/NN-short-name.md. One prompt, one job. Delete these comments. -->

AI-generated, unreviewed by a human. <!-- Drop this line once a person has read it. -->

## Context

Who is asking and why, in two or three sentences. The model, the tool and the date it was written with. What it
builds on (a repository, a commit, an ADR by number). Anything an agent starting cold must know.

## Goal

The outcome, in one or two sentences. State what is true when the job is done, not the steps.

## Read first

- the files, docs and ADRs to read before touching anything (paths, not summaries)
- the decisions that are settled. Say "do not reopen them".

## Scope

- **In:** what this job changes.
- **Out:** what it must not touch, and what it leaves for a later prompt.

## Constraints

- Smallest complete change. Name what you skipped.
- Every Python file starts with `# SPDX-License-Identifier: Apache-2.0`.
- Tests use synthetic data, never the network, a model server or real content.
- Do not commit or push. The owner confirms the provenance trailer first.
- <!-- the constraints specific to this job: versions, files, interfaces that must not change -->

## Steps

1. A step an agent can do and check. Name the test or command that shows it worked.
2. The next step.

## Done when

- [ ] the checks that must pass (`tools/QA.sh`, a named test)
- [ ] the docs that must change with the code
- [ ] the ADR written or amended, if a decision was made

## Questions

Put open questions in one block at the end: numbered, one bold line each, your default beside it.

## Report back

What changed, what you skipped, what you did not verify, and any risk the owner must know.
