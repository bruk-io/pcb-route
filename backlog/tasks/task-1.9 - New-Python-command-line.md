---
id: TASK-1.9
title: New Python command line
status: To Do
assignee:
  - '@bruk'
created_date: '2026-10-13 02:03'
updated_date: '2026-10-13 03:56'
labels:
  - size-3
milestone: m-3
dependencies:
  - TASK-1.6
  - TASK-1.7
  - TASK-1.8
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
route.py mixes the command line, the export cache, running the router process with signals and the time limit, and the order of the stages, and still speaks Freerouting's flags. The drop-in is no longer wanted. decision-1 leaves the command line as the only Python besides the KiCad reader, calling the core exactly as a library user would.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 pcb-route routes a KiCad board and reroutes it from a previous session, writing the session where asked
- [ ] #2 It calls only the public API through the extension, and holds no routing logic
- [ ] #3 It runs the KiCad reader as a child process under KiCad's Python, and keys and prunes the cache
- [ ] #4 Click in the bh-01 style; flags, log and exit codes are documented in the README
- [ ] #5 Full and incremental routes of the carrier pass KiCad DRC, and route.py is removed
<!-- AC:END -->
