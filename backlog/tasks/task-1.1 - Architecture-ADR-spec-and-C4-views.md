---
id: TASK-1.1
title: 'Architecture: ADR, spec and C4 views'
status: To Do
assignee:
  - '@bruk'
created_date: '2026-10-13 02:02'
labels:
  - size-3
dependencies: []
references:
  - >-
    backlog/decisions/decision-1 -
    Pipes-and-filters-with-a-Mojo-core-behind-a-Python-command-line.md
  - backlog/docs/doc-1 - Architecture-spec.md
  - architecture/
parent_task_id: TASK-1
type: design
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
TASK-1 asks for the architecture to be written down and accepted before any code moves. This task holds the three pieces for review: decision-1 (the pattern and the main choices), doc-1 (layout, Mojo layers, Python boundaries, file contracts, tests, tooling, proposed tasks) and the LikeC4 model in architecture/. Drafted on 12 Oct 2026; nothing has been refactored.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 decision-1 records the options considered, the decision, the consequences and the risks, and is accepted
- [ ] #2 doc-1 answers every open question in TASK-1, states each dependency rule with its reason, and gives each cross-process file one owner
- [ ] #3 The C4 views (landscape, context, containers, command-line components, reroute scenario) pass likec4 validate and c4lint with no errors or warnings
- [ ] #4 The proposed tasks in doc-1 are reviewed and created under TASK-1 with their dependencies
<!-- AC:END -->
