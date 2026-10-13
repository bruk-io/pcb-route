---
id: TASK-1.5
title: 'Grid in Mojo: rasterise, patch and store'
status: To Do
assignee:
  - '@bruk'
created_date: '2026-10-13 02:03'
updated_date: '2026-10-13 03:56'
labels:
  - size-8
milestone: m-1
dependencies:
  - TASK-1.4
references:
  - >-
    backlog/decisions/decision-1 -
    Pipes-and-filters-with-a-Mojo-core-behind-a-Python-command-line.md
  - backlog/docs/doc-1 - Architecture-spec.md
parent_task_id: TASK-1
type: task
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
board.py (about 1,050 lines of Python) turns the board into the router's grid and patches a previous export after an edit; the grid file it writes has no single owner or written layout. decision-1 moves this to a Mojo grid module that owns the store format. It is the riskiest move for what gets routed, so parity with today's exporter is the gate. Size-8: split it when it is started.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The Mojo grid built from a design file matches board.py's export cell for cell on the carrier and blinky, or each difference is explained and accepted in the task
- [ ] #2 A patched grid equals a full build for each kind of edit test_patch.py covers today
- [ ] #3 save_grid and load_grid own the store format, with a version and build options in the header; a mismatch is a cache miss, not an error; the layout is documented beside the module
- [ ] #4 board.py is removed once parity holds, and no remaining code reads grid bytes outside the grid module
- [ ] #5 Patching the carrier after the golden-to-kicad edit is no slower than today (1.6 s)
<!-- AC:END -->
