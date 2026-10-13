---
id: TASK-1.4
title: 'S-expression reader, design types, design-file spec and KiCad reader'
status: To Do
assignee:
  - '@bruk'
created_date: '2026-10-13 02:03'
updated_date: '2026-10-13 03:56'
labels:
  - size-5
milestone: m-1
dependencies:
  - TASK-1.2
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
pcbnew only runs under KiCad's Python 3.13, so decision-1 reads KiCad in a separate process that writes a design file, and the Mojo core reads that file. Today board.py reads pcbnew and rasterises in one module, so none of the core can be tested without KiCad. This task creates the seam: the design as data, its file format, and the only code that imports pcbnew.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 docs/design-file.md specifies the design file grammar, with a version field; the reader refuses an unknown version with a clear error
- [ ] #2 The design file carries everything today's exporter takes from pcbnew (layers, outline, nets and classes, pads, tracks, arcs, vias, zones, keep-outs, holes, board clearances)
- [ ] #3 pcbroute_kicad runs under KiCad's Python, imports only pcbnew and the stdlib, and writes design files for the carrier and blinky
- [ ] #4 The Mojo design reader reads those files; its tests start from checked-in design files and need no KiCad
- [ ] #5 The S-expression reader is its own module with no dependencies, ready for the Specctra reader to share
<!-- AC:END -->
