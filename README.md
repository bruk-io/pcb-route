# pcb-route

A fast PCB autorouter for KiCad boards that, above all, reroutes incrementally: given the previous
session, it keeps what an edit left standing and reroutes only what the edit disturbed. Nothing is
locked except hand-laid copper. It reads and writes Specctra files and takes Freerouting's command
line (`-de board.dsn -do board.ses -mp 40`), so it can stand in where Freerouting is called today.

The router is a negotiated-congestion grid router (bucket-queue A*, kept copper, via passes and a
straight-track cleanup) written in Mojo; `board.py` reads the KiCad board with KiCad 10's Python.
On the spotmicro carrier below it is about 6 times faster than Freerouting on a full route and
about 100 times faster rerouting after an edit, DRC-clean, with more vias than Freerouting.
It has been measured on that board and a small example only so far.

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

Log lines match Freerouting's: `Starting auto-routing...`, `Auto-routing was completed in N seconds.`,
`Saving '<file>'...`. SIGTERM finishes the net being routed and writes the result (a via pass under way is dropped). Exit code 0 when
every net is routed without conflict, 2 when something is left, 1 when nothing was written.

Pads wholly inside a track keep-out (under a pour) are left for the pour, as Freerouting leaves them;
the log lists them.

Needs KiCad 10's Python (`pcbnew`; set `KICAD_PYTHON` if it is not `/usr/bin/python3`), numpy,
matplotlib, and the router built with Mojo 1.1 (`pip install mojo`):
`mojo build -D ASSERT=none -o mojo/negotiate mojo/negotiate.mojo`. The tests also check it
against the C++ reference router, `g++ -O2 -std=c++20 -o negotiate6 negotiate6.cpp`. Board
exports are cached (`--cache`, default `~/.cache/pcb-dropin`), so the golden board is read once.
With `--previous`, the edited board is read as a patch of the previous board's export: only the
grid around items that changed (moved, re-netted, added, removed) is drawn again, and the result is
the same, byte for byte, as reading the whole board (`tests/test_patch.py` checks seven kinds of
edit). The cache keeps the 8 most recently used exports, about 190 MB each on the spotmicro carrier.

Tests: `python -m pytest tests` (about 6 minutes). The quick tests route pcbkit's blinky example,
from a pcbkit checkout beside this repository (or set `PCBKIT`); the rest use the spotmicro
carrier in `boards/spot`.

## pcbkit changes this implies

* `build_command`: the new command line above, `--board` = the board `pre` wrote the DSN from.
* `pcbkit route --eco`: pass `--previous golden/<stem>.ses` instead of copying and locking copper.
* The retry loop: pass `--seed <try number>`; results are otherwise deterministic.

## Files

* `route.py` - the command line
* `board.py` - KiCad 10 board to the router's grid (pre-routes, keep-outs, per-pad clearances)
* `incremental.py` - which of the previous session's pieces still stand
* `mojo/` - the router (negotiated congestion, bucket-queue A*, kept copper):
  `negotiate.mojo` the command line and the negotiation rounds, `board.mojo` the grid and nets,
  `congestion.mojo` prices and footprints, `search.mojo` the A* search, `cost.mojo` step costs,
  `buffer.mojo` lazily zeroed per-cell storage, `tidy.mojo` the staircase cleanup, `stop.mojo`
  SIGTERM, `mt19937.mojo` the seeded jitter
* `negotiate6.cpp` - the C++ router it was ported from, kept as the reference it must match
* `ses.py` - the session writer
* `check.py` - import a session with pcbnew and count KiCad DRC's copper problems
* `tests/test_route.py` - one test per item of the wrapper contract
* `tests/test_patch.py` - a patched board read equals a full read, for each kind of edit
* `tests/test_mojo.py` - with its improvements off, the Mojo router writes what the C++ one writes,
  byte for byte
* `tests/test_quality.py` - tidying and via passes keep the route conflict-free and do their job

## Results on the spotmicro carrier (12 Oct 2026, one 2-core cloud machine)

The edit: C2, C41, C50 and C51 moved (golden/ -> kicad/). Checked by importing each session into
pcbnew 10 and running KiCad DRC at every severity, before pours.

| Run | Wall time | Copper problems | Vias | Track pieces | Track |
| --- | --- | --- | --- | --- | --- |
| Freerouting 1.9.0, `-mp 40` | 337 s | 4 unconnected (3 connections at U8 left unrouted) | 58 | 1,453 | 4,490 mm |
| Ours, full route, cold cache | 55 s (43 s routing) | 0 | 98 | 1,588 | 3,961 mm |
| Ours, full route, `--via-cost 2.4` (no via passes), board export cached | 17 s | 0 | 169 | 1,520 | 3,612 mm |
| Ours, full route, `--via-cost 40`, board export cached | 91 s | 0 | 71 | 1,720 | 4,366 mm |
| Ours, `--previous golden/...`, golden cached (the usual case) | 3.5 s | 0 | 75 | 1,559 | 4,791 mm |

The incremental run keeps 96% of the previous (Freerouting) track; the edited board is read as a
patch in 1.6 s. Of our 98 vias, about 17 are on the three U8 connections Freerouting left
unrouted.

No router via lands in a surface-mount pad or within the board's hole-to-hole distance of an
existing hole; hand-laid tracks and vias are untouched (they are not written to the session).

## Known limits

* `--min-width` above the net classes' own widths widens every track; on this board that left two
  fine-pitch pads unreachable and 3 clearance faults. At or below the class widths (pcbkit's use:
  never below the board minimum) it is only a floor.
* A full route still uses more vias than Freerouting (98 against 58 on the spotmicro carrier, 71
  at `--via-cost 40` in about twice the time). Most of the rest are crossings: a track meets
  another net's track on its layer, and no detour cheaper than the via cost is clear.
* Reused copper is trusted where the edit did not change what may stand in its cells; a net whose
  class width or clearance changed is routed afresh.
* The time limit and SIGTERM stop after the net being routed; a SIGTERM before routing starts
  (while the board is read) leaves no session.

## Mojo and C++ routers (12 Oct 2026, spotmicro edit, 2-core cloud machine)

The Mojo router gives the same routes as the C++ router, byte for byte, for every net order, seed
jitter and the incremental run, and is about twice as fast. Same algorithm, same order of work;
the speed comes from how the work is done:

| Router | Full route | Incremental, wall (median of 5) |
| --- | --- | --- |
| Mojo, optimised | 23.5 s | 0.47 s |
| Mojo, straight port | 47.3 s | 1.00 s |
| C++, `g++ -O3 -march=native` | 48.1 s | 1.04 s |
| C++, `g++ -O2` | 48.8 s | 1.06 s |

Where the time went, and what changed (each change measured on its own, outputs compared every
time):

* Pricing a via site (the dearest cell under the via, on every layer) was over half the full
  route. It now scans each row of the via's disc in SIMD vectors. Where no copper covers a
  vector, a cell's price is exactly `1 + history`, so only the largest history is needed:
  49 s to 24 s together.
* The A* estimate divided twice per neighbour to find its row and column; neighbours now take
  them from the cell being expanded, and the one division left per expansion is 32-bit unsigned.
* A cell's best cost, "seen" and "closed" marks sit in one 16-byte record, so a visit reads one
  cache line, not three.
* Counting a net's keep-out footprint was 40% of an incremental run. Rows are tested 16 cells at
  a time and only unmarked cells are visited (bitmask, count-trailing-zeros). When a path cell is
  next to the one before it, only the edge of the disc that the step uncovers can hold unmarked
  cells, so only that is read.
* Page faults were a third of an incremental run's time. Grid-sized buffers are mapped with
  huge pages (Linux), the board's grids are read straight into them, and the owner map is
  computed in place: 200k faults down to 15k.
* Tried and dropped, since neither was faster: packing occupancy, kept and history into one
  record per class, and skipping a via's price when every other layer is already reached more
  cheaply (under 0.1% of sites).

The router now rejects a NaN or infinite price setting and a negative `weight_tenths`, which gave
no meaningful route before (a NaN price, or a crash).

Notes from the port:

* Mojo checks every collection index by default; `-D ASSERT=none` turns that off, as C++ has none.
* `List.read_bytes()` took 0.45 s on the 200 MB board file against 0.17 s for C's `fread`; the
  router reads into an uninitialised buffer instead.
* The Mojo runtime runs two threads of its own, so blocking SIGTERM on the main thread is not
  enough: a C-ABI handler passes a stop signal on to the main thread, which polls for it between
  nets. It stops within a net, as the C++ router does.
* The random net order (seed 4 and up) shuffles differently from libstdc++'s `std::shuffle`; seeds
  0-3 (and negative seeds, read as unsigned as C++ does) match. A 4-layer board is in the tests:
  an early version of the port overflowed a fixed step array there.
* An environment variable set to an empty value counts as unset (C++ reads it as 0), and numbers
  must parse cleanly; `route.py` sets neither case. The research-only diagnostics (reach report,
  `NEGOTIATE_HISTORY_IN/OUT`, `NEGOTIATE_CONFLICTS`, `NEGOTIATE_CHECK_SITES`) are not ported;
  the C++ router still has them.

## Route quality: tidying, via passes, lazy via pricing (12 Oct 2026)

Three router settings, all set by `route.py`; with none of them the router is still the C++
router, byte for byte (`tests/test_mojo.py`):

* `NEGOTIATE_TIDY` - once negotiation is over, each connection is redrawn between its fixed
  points (ends, vias, junctions) as runs of at most two pieces, one straight and one at 45
  degrees, wherever every cell of the run is clear of other copper. A grid A* path alternates
  straight and diagonal steps; this took the full route from 6,864 track pieces (5,019 under
  0.25 mm) to 1,520, in well under a second, without moving a via.
* `NEGOTIATE_VIA_PASS` (a via factor) and `NEGOTIATE_VIA_PASSES` - once negotiation has settled
  with cheap vias, vias get dearer, the nets with vias are routed again, and negotiation settles
  the board again; if it does not, or vias do not fall, the earlier result stands. Charging the
  higher via cost through every round instead costs 2-5 times the time for the same vias.
* `NEGOTIATE_LAZY_VIAS` - a via site is priced when the cheapest via there could be next on the
  path, not when its cell is expanded. The paths found are as cheap; the search took 30% less
  time at the cheap via cost and about 40% less in a via pass.

Where the time goes as the via cost rises: the search, not the arithmetic. Once a track is
blocked on its layer, A* has to try every detour cheaper than a via before it takes one, so the
cells searched grow with about the square of the via cost (79 M cells at 2.4 mm, 221 M at 16 mm,
520 M at 40 mm). Each cell searched costs about 190 ns. On the default full route: negotiation
17 s, the first via pass 20 s, the second 4 s, tidying under 1 s.

Tried and dropped: counting the via a cell's layer forces into the A* estimate (the target pad is
rarely on one layer only, so almost no gain), restarting the sharing price for a via pass (never
settled), and taking vias out while tidying through clear cells only (8 vias for 4 s; a via pass
does better).
