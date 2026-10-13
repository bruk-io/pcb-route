---
id: TASK-1.6
title: Reuse in Mojo
status: To Do
assignee:
  - '@bruk'
created_date: '2026-10-13 02:03'
labels:
  - size-3
dependencies:
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
incremental.py works out in Python which pieces of the previous route still stand on the edited grid, and hands them to the router through a preset file. decision-1 makes this a Mojo stage that returns a plan as a value, so the preset file goes away.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The Mojo reuse stage keeps the same pieces as incremental.py on the carrier edit, or each difference is explained
- [ ] #2 The plan crosses the API as a value; no preset file is written
- [ ] #3 Its tests run from design files and a session, without KiCad
- [ ] #4 incremental.py is removed
<!-- AC:END -->
