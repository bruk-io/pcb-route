---
id: decision-1
title: Pipes and filters with a Mojo core behind a Python command line
date: '2026-10-13 02:00'
status: proposed
---
## Context

TASK-1 sets the aim: Python only for the command line, Mojo for everything else as far as it can go, a core a library user could import later, and stated, enforced boundaries. Being a drop-in for Freerouting no longer matters, so the command line, log lines and exit codes may change. What gets routed must not.

What the work actually is shapes the choice:

- Routing is a pipeline of stages, each a function of the previous stage's output: a KiCad board becomes a design (layers, nets, pads, tracks, rules), the design becomes a grid, the grid and a previous session become a plan of what to keep, the grid and the plan become a route, the route becomes a session. Only the two ends touch the outside world.
- KiCad's pcbnew is a Python module tied to KiCad's own interpreter (Python 3.13 on this machine, KiCad 10.0.7). The house floor for new Python is 3.15. The two cannot share a process.
- Mojo 1.1 can build a Python extension module (`PythonModuleBuilder`, `PyInit_<name>`). The feature is in beta: no keyword arguments, limited conversions, and no packaging story for dependencies.
- Today the stages talk through files written by one language and read by another (the board grid, the preset, the dump), with no owner and no written layout. route.py runs the router as a process and signals it to stop.

Patterns considered:

1. **Layered (presentation, application, domain, infrastructure).** Fits services with a database behind them. Here the domain is a sequence of transforms with almost no infrastructure, so the layers would be mostly empty, and the grid, reuse and route stages would all land in one "domain" layer with no rule between them.
2. **Hexagonal (ports and adapters).** Earns its keep when the core has several interchangeable outside dependencies. This core has two edges, a design reader and a session writer, and each has one real implementation. Ports for every stage would be abstraction ahead of a second concrete case.
3. **Pipes and filters, with a functional core and imperative shell in each stage.** Matches the work: each stage is a filter over plain data, the dependency direction follows the pipeline, and the only effects are at the ends. Incremental rerouting is the same pipeline with a stored grid and a previous session fed in.

## Decision

Pipes and filters across the stages, functional core and imperative shell inside each, adapters only at the edges.

1. **Stages.** design → grid → reuse plan → route → session. Each is a Mojo module whose public functions take data and return data. File and process work lives in the command line and in the two edge readers and writers, never in a stage.
2. **The Mojo core is one package with one public API.** The command line uses it through a Python extension module that only translates calls; a library user would import the same API. Nothing outside the package reaches into a stage module.
3. **The command line is Python 3.15 and calls the core in process**, through the extension module. Falling back to a child process that speaks the same API over files is the alternative if the spike (below) fails.
4. **KiCad is read in a separate process**, a small Python program run under KiCad's interpreter that imports pcbnew and the standard library only, and writes a design file. It is the only code that imports pcbnew.
5. **The design file is S-expression text**, specified in the spec and read by the core's S-expression reader, which the Specctra reader shares. Its owner is the core's design module.
6. **The grid store belongs to the grid module**, which saves and loads it. The command line chooses where (cache keys, pruning) but never reads its bytes. The preset and dump files go away: kept copper and the route cross the API as values.
7. **Stopping.** The router asks a stop check between nets. In the command line the binding answers it from Python's pending signals, so Ctrl-C and SIGTERM still finish the net in hand and keep the result. The stop.mojo signal hack goes away.
8. **Dependency rules are enforced, each with its reason**: pypeeker import boundaries for Python, and a test that checks every Mojo module's imports against the layer table in the spec.
9. **The spike comes first.** Before code moves, a spike proves on the robot carrier that the extension module can take a design in, hold a grid between calls, be stopped by Ctrl-C, load under Python 3.15, and route as fast as the separate router does today.

## Consequences

- The KiCad reader is the only Python besides the command line; everything that is logic today in board.py, incremental.py and ses.py moves to Mojo. board.py's rasterising has to be rebuilt in Mojo and matched against today's exporter cell for cell before board.py is removed.
- The core's tests no longer need KiCad: they start from design files.
- Every cross-process file has one owner and a written spec: the design file (design module) and the grid store (grid module). There are no others.
- The command line, log and exit codes change. pcbkit has to move to the new command when it updates; Freerouting's flags are no longer accepted.
- A DSN-only route becomes a second design reader and nothing else, so it can be a later intent without changing this architecture.

Risks:

- Mojo's Python extension modules are beta. If the spike shows they cannot do what decision 3 needs, the command line runs the core as a child process instead, through the same API exposed as a small Mojo program; the stage boundaries do not change.
- Mojo's Python package may not support Python 3.15 yet. If not, the command line's floor drops to the newest version it does support, and the 3.15-only idioms wait.
- Stopping through Python's signal check is unproven from inside a Mojo call; the spike covers it.
- Rebuilding the rasteriser risks changing what gets routed. The parity test against today's exporter is the guard, and board.py stays until it passes.
