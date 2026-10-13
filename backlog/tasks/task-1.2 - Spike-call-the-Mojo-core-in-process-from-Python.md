---
id: TASK-1.2
title: 'Spike: call the Mojo core in process from Python'
status: To Do
assignee:
  - '@bruk'
created_date: '2026-10-13 02:03'
labels:
  - size-3
dependencies: []
references:
  - >-
    backlog/decisions/decision-1 -
    Pipes-and-filters-with-a-Mojo-core-behind-a-Python-command-line.md
  - backlog/docs/doc-1 - Architecture-spec.md
parent_task_id: TASK-1
type: spike
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
decision-1 has the Python command line call the Mojo core in process through a Mojo extension module. That feature is beta in Mojo 1.1 (no keyword arguments, limited conversions), Mojo may not load under Python 3.15, and stopping on Ctrl-C from inside a long Mojo call is unproven. Everything after this depends on the answer, so it is settled on the real workload before any code moves. If it fails, decision-1 names the fallback: the core runs as a child process behind the same API.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A Python 3.15 process (or the newest version Mojo supports, recorded) imports a Mojo extension module and routes the robot carrier from a grid built by today's exporter
- [ ] #2 A grid object built in one call is held by Python and used by later calls without being copied or written to disk
- [ ] #3 Ctrl-C or SIGTERM during routing finishes the net in hand and returns the route so far to Python
- [ ] #4 Routing time through the extension is within 5% of the separate router binary on the carrier, same output
- [ ] #5 The findings and the go or fallback call are recorded in the task, and decision-1 is updated if the fallback is taken
<!-- AC:END -->
