# pcb-route

A fast autorouter for KiCad boards that, above all, reroutes incrementally: given the previous
route, it keeps what an edit left standing and routes again only what the edit disturbed. Nothing
is locked except hand-laid copper. It reads a Specctra design and writes a Specctra session, on
Freerouting's command line (`-de board.dsn -do board.ses -mp 40`), so it can run where Freerouting
is called today.

On the one real board it has been measured on (a two-layer robot carrier, below), a full route
takes 55 s against Freerouting's 337 s, and rerouting after a small edit takes 3.5 s, both clean
under KiCad's DRC. It uses more vias than Freerouting (98 against 58). Freerouting can also keep
existing copper, routing only what is missing; that mode has not been timed against this one yet.

## Use

```
python route.py -de kicad/board.dsn -do kicad/board.ses -mp 40 --board kicad/prerouted.kicad_pcb
python route.py ... --previous golden/board.ses        # incremental; golden/prerouted.kicad_pcb is read too
```

| Flag | Meaning |
| --- | --- |
| `-de`, `-do`, `-mp` | As Freerouting: design in, session out, most negotiation rounds |
| `--board` | The KiCad board the DSN was written from (geometry and rules come from it) |
| `--previous` | A previous session of this board, to reuse |
| `--previous-board` | The board that session was routed on (default: `prerouted.kicad_pcb` beside it) |
| `--max-seconds` | No new round after this; the result so far is written (default 600) |
| `--min-width` | No track narrower than this, in mm |
| `--seed` | A different net order and starting prices, for a retry |
| `--via-cost` | Millimetres of track one via is worth in the finished route (default 16); raise it for fewer vias, at more time. Negotiation settles with cheap vias first (2.4 mm, or `--via-cost` if lower); via passes then reroute the nets that have vias at this cost |
| `--cache` | Where board exports are kept (default `~/.cache/pcb-route`, or `PCB_ROUTE_CACHE`) |

Log lines match Freerouting's: `Starting auto-routing...`, `Auto-routing was completed in N seconds.`,
`Saving '<file>'...`, plus `Router: <rounds> rounds, <vias> vias, <cells> cells searched`. SIGTERM
finishes the net being routed and writes the result (a via pass under way is dropped). Exit code 0
when every net is routed without conflict, 2 when something is left, 1 when nothing was written.

Pads wholly inside a track keep-out (under a pour) are left for the pour, as Freerouting leaves them;
the log lists them.

### Install

Needs KiCad 10's Python (`pcbnew`; set `KICAD_PYTHON` if it is not `/usr/bin/python3`), numpy,
matplotlib, and the router built with Mojo 1.1 (`pip install mojo`):

```
mojo build -D ASSERT=none -o router/negotiate router/negotiate.mojo
```

`-D ASSERT=none` turns off Mojo's bounds check on every collection index, which costs the router a
fair share of its speed. Mojo runs on Linux and macOS (on Windows, through WSL).

### Tests

`python -m pytest tests` (about 6 minutes). The quick tests route pcbkit's blinky example, from a
pcbkit checkout beside this repository (or set `PCBKIT`); the rest use the robot carrier in
`boards/spot`. `tests/test_snapshots.py` pins the router's output; a change that alters it fails
until accepted with `UPDATE_SNAPSHOTS=1` (the test's docstring says when that is right).

## How it works

1. `board.py` turns the KiCad board into a grid (0.05 mm cells per layer): for each net class,
   which net may stand in each cell, where it may drill, and each net's pads. Exports are cached;
   after an edit, the edited board is read as a patch of the previous export, redrawing only the
   grid around what changed, with the same result as a full read (`tests/test_patch.py`).
2. With `--previous`, `incremental.py` works out which pieces of the previous route still stand on
   the edited grid; they are offered to the router as kept copper.
3. The router (`router/`) routes every net with A* on the grid, then negotiates: cells two nets
   share get dearer every round, and the nets in conflict are routed again, until none share a
   cell. Kept copper is laid first and is only routed afresh if other nets keep running over it.
4. Via passes: once the board has settled with cheap vias, vias get dearer, the nets that have
   them are routed again, and negotiation settles the board again. If it does not, or vias do not
   fall, the earlier result stands.
5. Tidying: a grid path alternates straight and diagonal steps. Each connection is redrawn between
   its fixed points (ends, vias, junctions) as runs of at most two pieces, one straight and one
   at 45 degrees, wherever every cell of the run is clear of other copper.
6. `ses.py` writes the session; reused pieces are written as they were.

## Results on the robot carrier (12 Oct 2026, one 2-core cloud machine)

The board: a 127 x 95 mm two-layer carrier with about 160 parts. The edit: C2, C41, C50 and C51
moved (`boards/spot/golden` to `boards/spot/kicad`). Each session was imported into pcbnew 10 and
checked with KiCad DRC at every severity, before pours.

| Run | Wall time | Copper problems | Vias | Track pieces | Track |
| --- | --- | --- | --- | --- | --- |
| Freerouting 1.9.0, `-mp 40`, full route | 337 s | 4 unconnected (3 connections at U8 left unrouted) | 58 | 1,453 | 4,490 mm |
| pcb-route, full route, cold cache | 55 s (43 s routing) | 0 | 98 | 1,588 | 3,961 mm |
| pcb-route, full route, `--via-cost 2.4` (no via passes), board export cached | 17 s | 0 | 169 | 1,520 | 3,612 mm |
| pcb-route, full route, `--via-cost 40`, board export cached | 91 s | 0 | 71 | 1,720 | 4,366 mm |
| pcb-route, `--previous golden/...`, golden export cached | 3.5 s | 0 | 75 | 1,559 | 4,791 mm |

The incremental run keeps 96% of the previous route's track; the edited board is read as a patch
in 1.6 s. Of the full route's 98 vias, about 17 are on the three U8 connections Freerouting left
unrouted. No router via lands in a surface-mount pad or within the board's hole-to-hole distance
of an existing hole; hand-laid tracks and vias are untouched (they are not written to the session).

## Known limits

* Measured on one real board and one small example. Dense 4- to 6-layer boards and fine-pitch
  BGAs are untried.
* More vias than Freerouting. Most of the rest are crossings: a track meets another net's track
  on its layer, and no detour cheaper than the via cost is clear.
* No length matching, differential pairs or impedance control.
* `--min-width` above the net classes' own widths widens every track; on the carrier that left two
  fine-pitch pads unreachable and 3 clearance faults. At or below the class widths it is only a
  floor.
* Reused copper is trusted where the edit did not change what may stand in its cells; a net whose
  class width or clearance changed is routed afresh.
* The time limit and SIGTERM stop after the net being routed; a SIGTERM before routing starts
  (while the board is read) leaves no session.
* The grid takes memory: about 190 MB per export of the carrier, and about 0.7 GB while routing.

## Use from pcbkit

This began as the router for pcbkit, a board-as-code tool (`INTENT.md` has the
problem it set out to solve). There:

* `build_command`: the command line above, `--board` = the board `pre` wrote the DSN from.
* `pcbkit route --eco`: pass `--previous golden/<stem>.ses` instead of copying and locking copper.
* The retry loop: pass `--seed <try number>`; results are otherwise deterministic.

## Files

* `route.py` - the command line
* `board.py` - KiCad 10 board to the router's grid (pre-routes, keep-outs, per-pad clearances)
* `incremental.py` - which of the previous session's pieces still stand
* `router/` - the router: `negotiate.mojo` the command line, negotiation and via passes,
  `board.mojo` the grid and nets, `congestion.mojo` prices and footprints, `search.mojo` the A*
  search, `tidy.mojo` the straightening, `cost.mojo` step costs, `buffer.mojo` lazily zeroed
  per-cell storage, `stop.mojo` SIGTERM, `mt19937.mojo` the seeded jitter
* `ses.py` - the session writer
* `check.py` - import a session with pcbnew and count KiCad DRC's copper problems
* `tests/test_route.py` - the command line's contract
* `tests/test_patch.py` - a patched board read equals a full read, for each kind of edit
* `tests/test_snapshots.py` - the router's output, pinned
* `tests/test_quality.py` - tidying and via passes keep the route conflict-free, and full and
  incremental routes pass KiCad DRC

## How the router got fast (12 Oct 2026, robot carrier)

The router began as a C++ program. Its first Mojo version was a straight port that matched it byte
for byte at the same speed (a full route in 47 s against 48 s); the changes below, each measured on
its own with the output compared every time, then halved that, before via passes and tidying were
added:

* Pricing a via site (the dearest cell under the via, on every layer) was over half a full route.
  Each row of the via's disc is scanned in SIMD vectors, and where no copper covers a vector, a
  cell's price is exactly `1 + history`, so only the largest history is needed.
* A via is priced only when its turn comes in the search: it is queued at the cheapest a via can
  be, and most queued vias never come up. The paths found are as cheap; the search takes 30% less
  time, and about 40% less in a via pass.
* The A* estimate divided twice per neighbour to find its row and column; neighbours take them
  from the cell being expanded, and the one division left per expansion is 32-bit unsigned.
* A cell's best cost, "seen" and "closed" marks sit in one 16-byte record, so a visit reads one
  cache line, not three.
* Counting a net's keep-out footprint was 40% of an incremental run. Rows are tested 16 cells at
  a time and only unmarked cells are visited; when a path cell is next to the one before it, only
  the edge of the disc that the step uncovers can hold unmarked cells.
* Page faults were a third of an incremental run's time. Grid-sized buffers are mapped with huge
  pages (Linux), and the board's grids are read straight into them: 200k faults down to 15k.

Where the time goes as `--via-cost` rises: the search, not the arithmetic. Once a track is blocked
on its layer, A* has to try every detour cheaper than a via before it takes one, so the cells
searched grow with about the square of the via cost (79 M cells at 2.4 mm, 221 M at 16 mm, 520 M
at 40 mm), at about 190 ns each. On the default full route: negotiation 17 s, the first via pass
20 s, the second 4 s, tidying under 1 s. Charging the higher via cost through every negotiation
round instead of in via passes costs 2-5 times the time for the same vias.

Tried and dropped: packing occupancy, kept copper and history into one record per class (no
faster); skipping a via's price when every other layer is already reached more cheaply (under 0.1%
of sites); counting the via a cell's layer forces into the A* estimate (target pads are rarely on
one layer only); restarting the sharing price for a via pass (never settled); taking vias out
while tidying through clear cells only (8 vias for 4 s; a via pass does better).

Notes on Mojo 1.1: `List.read_bytes()` took 0.45 s on the 200 MB board file against 0.17 s for C's
`fread`, so grids are read into buffers directly. The Mojo runtime runs threads of its own, so
blocking SIGTERM on the main thread is not enough: a C-ABI handler passes a stop signal on to the
main thread, which polls for it between nets. An environment variable set to an empty value counts
as unset, and numbers must parse cleanly.
