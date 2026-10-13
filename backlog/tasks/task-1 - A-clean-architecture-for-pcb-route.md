---
id: TASK-1
title: A clean architecture for pcb-route
status: To Do
assignee:
  - '@bruk'
created_date: '2026-10-13 01:49'
updated_date: '2026-10-13 01:56'
labels:
  - size-8
dependencies: []
type: intent
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
## Problem
The repository grew out of an experiment: a drop-in for Freerouting inside pcbkit, then a C++ router ported to Mojo. Now it is a public project with a different aim: a router that works as a command line today and can be exported as a library to use elsewhere later. Being a drop-in for Freerouting no longer matters, so its command line, log lines and exit codes may change.

The shape it grew into is in the way:
- Most of the logic is Python, though Mojo is meant to carry everything except the command line. board.py (about 1,050 lines) reads the KiCad board through pcbnew, turns pads and tracks into grid cells, and patches a previous export, all in one module, so the purest logic cannot be tested without KiCad installed. incremental.py and ses.py hold more core logic in Python.
- The files passed between stages (the board grid, the preset, the dump) have no single owner: the board file is written in board.py and read in router/board.mojo, incremental.py and a test; the dump is written by the router and decoded in ses.py. Nothing documents their layout.
- route.py mixes the command line, the export cache, running the router process with signals and the time limit, and the order of the stages.
- It is not a Python package: tests reach the modules by adding paths to sys.path.
- Nothing in it states or enforces my code standards (the engineering philosophy and the Python idioms).

Wanted: Python only for the command line; Mojo for the rest, as far as possible; boundaries that are stated and enforced, so each part can be read, tested and changed on its own; and a core whose public surface a library user could import, without changing what the router routes.

## Open questions
- [ ] Which architectural pattern
- [ ] How the Python command line calls the Mojo core (in-process extension module, or a separate process)
- [ ] Where KiCad's pcbnew is read, given it is tied to KiCad's own Python
- [ ] The package names and folder layout, for the command line and for the Mojo core
- [ ] How the import rules are enforced on the Python side and on the Mojo side
- [ ] Whether check.py stays a development tool or becomes a command
- [ ] Whether reading a design from the DSN alone (without KiCad) is in scope or a later intent
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The architecture is recorded as an ADR in backlog/decisions, a spec in backlog/docs and orthodox C4 views in LikeC4, accepted before any code moves
- [ ] #2 Python is used only for the command line and for reading KiCad through pcbnew; everything else is Mojo
- [ ] #3 The Mojo core has one public API that the command line uses exactly as a library user would
- [ ] #4 Every dependency rule the ADR states is checked by a test or lint that fails when it is broken and says why the rule exists
- [ ] #5 Each file passed between processes has one owner and a written specification
- [ ] #6 The core logic has tests that run without KiCad installed
- [ ] #7 The Python code follows the house Python idioms and tooling (uv, ruff, mypy --strict, pytest with a src layout, pypeeker), and the repository writes its conventions down
- [ ] #8 Must not break: what gets routed. The grid built for a board matches today's exporter cell for cell, or each difference is explained and accepted, and the snapshot and DRC tests pass, with snapshots updated only for explained differences
<!-- AC:END -->
