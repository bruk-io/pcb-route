---
id: TASK-1.10
title: Import rules for Python and Mojo
status: To Do
assignee:
  - '@bruk'
created_date: '2026-10-13 02:03'
updated_date: '2026-10-13 03:56'
labels:
  - size-1
milestone: m-0
dependencies:
  - TASK-1.3
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
doc-1 states which Mojo module may import which, and which Python package may import what, each with a reason. A boundary nobody checks drifts, so each rule becomes a check that fails with its reason.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 pypeeker import boundaries enforce the Python rules in doc-1
- [ ] #2 A test checks every Mojo module's imports against the layer table in doc-1
- [ ] #3 Each failure message says which rule broke and why the rule exists
- [ ] #4 Both run in the default test and lint commands
<!-- AC:END -->
