# Read this first: how the research folder is used

This folder is where PrivatePartyNet's plans and the research behind them are shared, so everyone working on the project,
people and their coding agents alike, can see what is planned before it is built. Read this page before using anything in
here, then the README of the newest month folder.

## What is in here

- One folder per month, named `YYYY-MM` for the month a document was written. Each month folder has a README listing every
  file in it and the status of each plan.
- Plans and research only. The app's own documentation (the main README, SECURITY.md, CONTRIBUTING.md and the How page in
  the app) describes what PrivatePartyNet does today; when a plan here and the app disagree, the app is what exists.
- A selection, not everything: the current, forward-facing plans and the research they rest on. Working notes stay with
  whoever wrote them until they are worth sharing.

## Kinds of document

- `SPEC-<date>_<topic>.md`: requirements, in K's own words where he gave them, with each round of his answers recorded as
  he wrote them. The spec is what a design has to satisfy.
- `DESIGN-<date>_<topic>.md`: how the requirements would be met, with the order of work and the questions still open.
- `PLAN-<date>_<topic>.md`: the steps for one piece of work (for example the Discord bridge).
- `<date>_<Title>.md`: a research file. Its header says who ran it, when, the question, and where it is used. Every finding
  says where it came from: "search summary" means a search tool's summary of pages found, "fetched" means the page itself was
  read. Findings marked "not checked" or "from memory" are leads, not facts.

## Status of a plan

The month README gives each plan one of these:

- Proposal: written for review. Nothing in it is built yet, and nothing should be built from it.
- Settled: K has approved it (with any changes he asked for). It can be built, in the order of work it gives.
- Built: it is in a release. From then on the app and its documentation are the authority; the plan stays as the record of
  why it was built that way.
- Superseded: a newer document replaces it; its status line names which one.

K decides when a proposal is settled. His answers go into the spec's rounds, and the open questions sit at the end of the
design or plan they belong to.

## For agents working on the project

- Before starting work on anything a plan here covers, read the plan and its spec, check its status, and work only from
  settled plans unless K or Nate has asked for something else.
- Treat each research finding by its label. Versions, prices, limits and policies change; confirm one at its source before
  relying on it.
- Do not rewrite someone else's plan to change what it says. To answer or disagree, add a file in the current month's folder
  named `RESPONSE-<date>_<topic>.md` with who wrote it at the top, or open a GitHub issue. Fixing typos and broken links is
  fine.

## Adding documents

- Put a document in the folder of the month it was written, add it to that month's README with its status, and follow the
  naming above.
- Running rule: plans and research kept elsewhere (for example in someone's own working notes) are copied here once they are
  more than a month old, if they are still worth sharing.
- Everything here is public. Before committing: no earlier product names and no personal paths or email addresses
  (`tests/test_rename.py` checks every published file and fails on them), nothing personal about anyone, and no secrets,
  keys or tokens.
