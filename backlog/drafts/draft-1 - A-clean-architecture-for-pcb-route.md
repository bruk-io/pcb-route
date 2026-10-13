---
id: DRAFT-1
title: A clean architecture for pcb-route
status: Draft
assignee:
  - '@bruk'
created_date: '2026-10-13 01:49'
labels:
  - size-8
dependencies: []
type: intent
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
## Problem
The repository grew out of an experiment: a drop-in for Freerouting inside pcbkit, then a C++ router ported to Mojo. Now it is a public project, and the shape it grew into makes it hard to see what depends on what, to test parts on their own, or to change one stage without touching others:
- board.py (about 1,050 lines) reads the KiCad board through pcbnew, turns pads and tracks into grid cells, and patches a previous export, all in one module; so the purest logic cannot be tested without KiCad installed.
- The binary files that pass between stages (the board grid, the preset, the dump) have no single owner: the board file is written in board.py and read in router/board.mojo, incremental.py and a test; the dump is written by the router and decoded in ses.py. Nothing documents their layout.
- route.py mixes the command line, the export cache, running the router process with signals and the time limit, and the order of the stages.
- It is not a Python package: tests reach the modules by adding paths to sys.path.

Wanted: an architecture whose boundaries are stated and enforced, so each part can be read, tested and changed on its own, without changing what the router does.

## Open questions
- [ ] Which architectural pattern (proposed in conversation: pipes and filters across the stages, a functional core and imperative shell inside each, adapters only at KiCad, Specctra and the router process)
- [ ] The Python package name and layout
- [ ] Which tool enforces the import rules (import-linter, pypeeker, or a script)
- [ ] Whether check.py stays a development tool or becomes a command
- [ ] Whether reading a design from the DSN alone (without KiCad) is in scope or a later intent
- [ ] Whether the Mojo router changes beyond what the file-format contracts need
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The architecture is recorded as an ADR in backlog/decisions, with orthodox C4 views in LikeC4, before any code moves
- [ ] #2 Every dependency rule the ADR states is checked by a test or lint that fails when it is broken, and says why the rule exists
- [ ] #3 pcbnew is imported in one module only
- [ ] #4 Each file passed between stages is written and read in one Python module, against a written specification that the Mojo reader follows
- [ ] #5 The grid and reuse logic have tests that run without KiCad installed
- [ ] #6 Must not break: tests/test_snapshots.py, tests/test_patch.py and the DRC tests pass unchanged, so the router's output is byte-identical
- [ ] #7 Must not break: route.py's command line, log lines and exit codes, which pcbkit relies on
<!-- AC:END -->
