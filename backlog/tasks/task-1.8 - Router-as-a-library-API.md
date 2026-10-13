---
id: TASK-1.8
title: Router as a library API
status: To Do
assignee:
  - '@bruk'
created_date: '2026-10-13 02:03'
labels:
  - size-3
dependencies:
  - TASK-1.2
  - TASK-1.5
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
The router is a program today: settings come from NEGOTIATE_* environment variables, kept copper from a preset file, the result goes to a dump file, and stop.mojo blocks signals so a SIGTERM finishes the net in hand. decision-1 makes it a function on the public API that a library user can call.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 route() takes a grid, a reuse plan, a settings value with defaults and a stop check, and returns the route as a value
- [ ] #2 No NEGOTIATE_* environment variables, preset or dump files remain
- [ ] #3 stop.mojo is gone; the stop check is asked between nets and the extension answers it from Python's pending signals
- [ ] #4 The snapshot tests pass unchanged, or each update is explained
<!-- AC:END -->
