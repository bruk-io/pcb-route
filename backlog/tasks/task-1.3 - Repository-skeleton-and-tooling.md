---
id: TASK-1.3
title: Repository skeleton and tooling
status: To Do
assignee:
  - '@bruk'
created_date: '2026-10-13 02:03'
labels:
  - size-2
dependencies: []
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
The repository is not a package: tests reach modules by adding paths to sys.path, and nothing states or runs the house standards. doc-1 sets the layout (src/pcbroute, src/pcbroute_cli, src/pcbroute_kicad) and the tooling. This puts the skeleton and the commands in place so every later task lands into it.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 pyproject.toml declares the package with a src layout and a `pcb-route` console script; tests import without touching sys.path
- [ ] #2 `uv run pytest`, `uv run ruff check`, `uv run typecheck` (mypy --strict) and `uv run pypeeker check --strict` run and pass on the skeleton
- [ ] #3 ruff enables pydoclint so undocumented raised exceptions fail the lint
- [ ] #4 One command builds the Mojo package and extension, release builds with -D ASSERT=none
- [ ] #5 docs/conventions.md writes down the conventions and CLAUDE.md points to it
- [ ] #6 The existing tests still pass
<!-- AC:END -->
