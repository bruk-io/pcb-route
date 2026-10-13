---
id: TASK-1.7
title: Specctra session read and write in Mojo
status: To Do
assignee:
  - '@bruk'
created_date: '2026-10-13 02:03'
updated_date: '2026-10-13 03:56'
labels:
  - size-3
milestone: m-2
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
ses.py writes the session in Python by decoding the router's dump file, and reading a previous session is part of incremental.py. decision-1 moves both into a Mojo specctra module that shares the S-expression reader, so the dump file goes away.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Sessions written by the Mojo writer are byte-identical to ses.py's for the same route, or each difference is explained
- [ ] #2 Reused pieces are written as they were in the previous session
- [ ] #3 A previous session is read through the shared S-expression reader
- [ ] #4 ses.py is removed and no dump file is written
<!-- AC:END -->
