---
id: doc-1
title: Architecture spec
type: specification
created_date: '2026-10-13 02:01'
updated_date: '2026-10-13 02:01'
---
# Architecture spec: pcb-route

Implements decision-1 (pipes and filters with a Mojo core behind a Python command line) for TASK-1. The C4 views are in `architecture/` (LikeC4): the landscape, the system context, the containers, the command line's components, and the reroute scenario.

## Containers

| Container | Runs as | Language | Responsibility |
| --- | --- | --- | --- |
| Command line | the `pcb-route` process | Python 3.15, Click, plus the Mojo core as an extension module | Arguments, cache keys and pruning, running the KiCad reader, the log and exit code; routes through the core's public API in process |
| KiCad reader | a child process under KiCad's Python | Python (floor 3.13), pcbnew and stdlib only | Reads a board and its design rules, writes a design file. The only code that imports pcbnew |
| Cache | files under `~/.cache/pcb-route` (or `PCB_ROUTE_CACHE`) | - | Design files and grids, keyed by board |

## Layout

```
src/
  pcbroute/                 Mojo core package (public API in __init__.mojo)
    support/                buffers, stop check, the seeded random generator
    sexpr/                  S-expression reader and writer
    design/                 the design types and the design-file reader
    grid/                   rasterise, patch, store (save and load)
    reuse/                  which previous pieces still stand
    route/                  negotiation, search, via passes, tidying
    specctra/               session reader and writer
    python/                 the extension module (bindings only)
  pcbroute_cli/             Python command line; console script `pcb-route`
  pcbroute_kicad/           Python KiCad reader, run under KiCad's interpreter
tests/                      pytest, including the Mojo test runner
tools/check_drc.py          development tool (PEP 723): import a session, count KiCad DRC problems
architecture/               LikeC4 model and views
boards/                     test boards
```

## Mojo layers

A module may import only the modules in its row. Enforced by a test that reads every module's imports and fails with the reason below.

| Module | May import | Why |
| --- | --- | --- |
| support | nothing | Leaf utilities; anything else would make them stage-specific |
| sexpr | nothing | A format, not a stage; shared by design and specctra |
| design | sexpr | The design file is S-expressions; design is the vocabulary every stage shares |
| grid | design, support | Rasterises designs; must not know how they are routed |
| reuse | grid, design | Checks old pieces against the new grid; never routes |
| route | grid, design, support | Routes on a grid; must not know where grids or designs come from |
| specctra | design, sexpr | Names nets and layers from the design; never sees the grid |
| api (package root) | all of the above | The one public surface |
| python | api only | A library user's view; bindings hold no logic |

## Python boundaries

Enforced with pypeeker (`[tool.pypeeker] import-boundaries`), each rule with its reason:

- `pcbroute_cli` imports the Mojo extension and nothing from `pcbroute_kicad`: the reader runs under another interpreter and is reached only as a child process.
- `pcbroute_kicad` imports pcbnew and the standard library only: KiCad's Python has nothing else installed.
- Nothing outside `pcbroute_kicad` imports pcbnew.

## Public API (shape, settled in the spike)

- `read_design(path) -> Design`
- `build_grid(design, options) -> Grid`; `patch_grid(old_grid, old_design, design, options) -> Grid`
- `save_grid(grid, path)`; `load_grid(path) -> Grid`
- `plan_reuse(grid, design, previous_session_text) -> Plan`
- `route(grid, plan, settings, stop) -> Route`
- `write_session(design, route, plan) -> String`

Settings are a value with defaults, not environment variables. The `NEGOTIATE_*` environment variables go away. The command line writes the session text where it was asked to.

## File contracts

**Design file** (owner: design). S-expression text, UTF-8, one file per board read. Written by the KiCad reader, read by `read_design`. It carries what today's exporter takes from pcbnew: layer stack, board outline, nets and net classes (width, clearance, via size and drill), pads (shape, size, rotation, layers, net, per-pad clearance), existing tracks, arcs and vias (marked hand-laid or not), zones and keep-outs, holes, and the board's hole-to-hole and edge clearances. A version field leads the file; a reader refuses a version it does not know. The exact grammar is written as `docs/design-file.md` with the first reader, and the reader's tests are its examples.

**Grid store** (owner: grid). Binary, written and read only by `save_grid` and `load_grid`, with a version and the options it was built with in a header; a mismatch is a miss, not an error. Its layout is documented beside the module. Nothing else reads it.

## Behaviour that stays

- What gets routed: the grid matches today's exporter cell for cell, or each difference is explained and accepted.
- Kept copper, via passes, tidying, seeds and determinism.
- Stopping on Ctrl-C or SIGTERM finishes the net in hand and writes the result.

Free to change: flags, log lines, exit codes, file names in the cache.

## Tests

- Mojo unit tests (`std.testing` TestSuite) per module, run from pytest so one command runs everything. They start from design files in `tests/designs/` and need no KiCad.
- Grid parity: during the migration, the Mojo grid against board.py's export, cell for cell, on the robot carrier and blinky, for full reads and for each kind of patch.
- Snapshots of the router's output (`tests/snapshots.json`), updated only for explained differences.
- Integration: the command line end to end, and KiCad DRC on the result (marked, skipped without KiCad).

## Tooling and conventions

- uv is the only interface: `uv run pytest`, `uv run ruff check`, `uv run typecheck` (mypy --strict), `uv run pypeeker check --strict`.
- ruff with pydoclint (documented exceptions), mypy --strict, pytest with a src layout, Click in the bh-01 style.
- Python follows the house idioms: frozen dataclasses, enums, closed TypedDicts, match with assert_never, PEP 695 generics, private by default.
- Mojo builds with `-D ASSERT=none` for release and with assertions in tests.
- `docs/conventions.md` writes these down; CLAUDE.md points to it.

## Proposed tasks

| # | Task | Type | Size | Depends on |
| --- | --- | --- | --- | --- |
| 1 | Spike: in-process Mojo from Python (design in, grid held between calls, Ctrl-C, Python 3.15, speed parity on the carrier) | spike | size-3 | - |
| 2 | Repository skeleton and tooling (pyproject, src layout, uv commands, conventions doc) | task | size-2 | - |
| 3 | S-expression reader, design types, design-file spec and the KiCad reader | task | size-5 | 1, 2 |
| 4 | Grid in Mojo (rasterise, patch, store) with cell-for-cell parity, then remove board.py | task | size-8, split when started | 3 |
| 5 | Reuse in Mojo, then remove incremental.py | task | size-3 | 4 |
| 6 | Specctra read and write in Mojo, then remove ses.py | task | size-3 | 3 |
| 7 | Router as a library API: settings as values, no preset or dump files, stop check instead of stop.mojo | task | size-3 | 1, 4 |
| 8 | The new Python command line, replacing route.py | task | size-3 | 5, 6, 7 |
| 9 | Import rules for Python and Mojo, with reasons | task | size-1 | 2 |

## Answers to TASK-1's open questions

- Pattern: pipes and filters, functional core and imperative shell per stage (decision-1).
- How the command line calls the core: in process through a Mojo extension module, after the spike; a child process is the fallback.
- Where pcbnew is read: a separate reader process under KiCad's Python, writing a design file.
- Names and layout: `pcbroute` (Mojo), `pcbroute_cli`, `pcbroute_kicad` under `src/`.
- Import rules: pypeeker for Python, a layer-table test for Mojo.
- check.py: stays a development tool, as `tools/check_drc.py`.
- DSN-only input: a later intent; it would be a second design reader.
