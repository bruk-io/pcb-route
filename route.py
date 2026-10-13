"""A drop-in for Freerouting's command line: read a Specctra DSN and a KiCad board, write a session.

    python route.py -de board.dsn -do board.ses [-mp 40] --board prerouted.kicad_pcb
                    [--previous golden/board.ses [--previous-board golden/prerouted.kicad_pcb]]
                    [--max-seconds 600] [--min-width 0.2] [--seed 0] [--via-cost 16]

What it keeps from Freerouting's contract (pcbkit's route/freerouting.py relies on each):
  * the -de / -do / -mp flags; the session imports with pcbnew's ImportSpecctraSES;
  * the log lines "Starting auto-routing...", "Auto-routing was completed in N seconds." and
    "Saving '<file>'...";
  * a time limit (--max-seconds): no new round starts after it, and the result so far is written;
    SIGTERM does the same at once;
  * --min-width: no track is narrowed below it (or the board's own minimum, if larger);
  * --seed: a different net order, for a retry that searches differently (results are deterministic).

Geometry and rules come from the KiCad board (--board), which closes the DSN's gaps (per-pad
clearances); the DSN supplies the session's name, units and via padstack names.

With --previous, the previous session's route is reused where the edit left it standing, and only
what the edit disturbed is routed again (see incremental.py). Nothing is locked but hand-laid copper.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import incremental  # noqa: E402
import ses  # noqa: E402

ROUTER = HERE / "mojo" / "negotiate"  # the Mojo router; negotiate6.cpp is the reference it must match
EXPORTER = HERE / "board.py"
KICAD_PYTHON = os.environ.get("KICAD_PYTHON", "/usr/bin/python3")
CACHE_KEEP = 8  # board exports kept (about 190 MB each on the spotmicro carrier); the least recently used go
ORDERS = ("span_asc", "pads_desc", "span_desc", "area_desc")
FULL_SCHEDULE = ("0.2", "1.3", "0.3")  # gentle: more clean boards on a full route (experiment 7)
INCREMENTAL_SCHEDULE = ("0.5", "1.6", "0.5")  # few nets move; a firmer schedule settles them sooner
NEGOTIATION_VIA_MM = 2.0  # the export's via cost; negotiation needs cheap vias to settle
VIA_FACTOR = 1.2  # negotiation's vias cost this many times NEGOTIATION_VIA_MM
VIA_PASSES = 2  # the first pass does most; a third rarely takes out more than a via (spotmicro)


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S") + f" [main] INFO  {message}", flush=True)


def _digest(*paths: Path, extra: str = "") -> str:
    digest = hashlib.sha256(extra.encode())
    for path in paths:
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:24]


def export(board: Path, options: dict, cache: Path, previous: Path | None = None) -> tuple[Path, dict]:
    """The router's grid for `board`, from the cache when the board, its project and the options match.

    Given the grid of an earlier version of the board (`previous`), only the area around what changed
    is read again; the result is the same as a full read.
    """
    project = board.with_suffix(".kicad_pro")
    key = _digest(board, project, EXPORTER, extra=json.dumps(options, sort_keys=True))
    out, meta_path = cache / f"{key}.board", cache / f"{key}.meta.json"
    if not (out.exists() and meta_path.exists()):
        cache.mkdir(parents=True, exist_ok=True)
        options_path = cache / f"{key}.options.json"
        options_path.write_text(json.dumps(options))
        command = [KICAD_PYTHON, str(EXPORTER), "export", str(board), str(out), str(meta_path), str(options_path)]
        done = subprocess.run(command + ([str(previous)] if previous else []), capture_output=True, text=True)
        if not meta_path.exists():
            raise SystemExit(f"could not read {board}: {done.stderr.strip().splitlines()[-1:] if done.stderr else 'no output'}")
    os.utime(out)  # used now: pruning keeps the most recently used
    _prune(cache)
    meta = json.loads(meta_path.read_text())
    if meta.get("skipped"):
        raise SystemExit(f"cannot route {board}: {meta['skipped']}")
    return out, meta


def _prune(cache: Path) -> None:
    """Keep the cache to the CACHE_KEEP most recently used exports, so frequent edits cannot fill the disk."""
    exports = sorted(cache.glob("*.board"), key=lambda path: path.stat().st_mtime, reverse=True)
    for old in exports[CACHE_KEEP:]:
        for path in cache.glob(old.stem + ".*"):
            path.unlink(missing_ok=True)


def previous_board_copy(previous_board: Path, project: Path, cache: Path) -> Path:
    """The previous board next to a project file, so its export sees the same net classes.

    pcbkit's golden/ folder has no .kicad_pro; the current project's is used for it.
    """
    if previous_board.with_suffix(".kicad_pro").exists() or not project.exists():
        return previous_board
    folder = cache / ("previous-" + _digest(previous_board, project))
    folder.mkdir(parents=True, exist_ok=True)
    copy = folder / previous_board.name
    if not copy.exists():
        shutil.copy(previous_board, copy)
        shutil.copy(project, copy.with_suffix(".kicad_pro"))
    return copy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-de", dest="dsn", required=True)
    parser.add_argument("-do", dest="ses", required=True)
    parser.add_argument("-mp", dest="passes", type=int, default=0, help="most negotiation rounds (Freerouting's passes)")
    parser.add_argument("--board", required=True, help="the KiCad board the DSN was written from")
    parser.add_argument("--previous", help="a previous session of this board, to reuse")
    parser.add_argument("--previous-board", help="the board that session was routed on (default: prerouted.kicad_pcb beside it)")
    parser.add_argument("--max-seconds", type=float, default=600.0)
    parser.add_argument("--min-width", type=float, default=0.0, help="mm; never route narrower")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--via-cost", type=float, default=16.0,
                        help="mm of track one via is worth in the finished route; raise it for fewer vias, at more time")
    parser.add_argument("--pitch", type=float, default=0.05)
    parser.add_argument("--cache", default=os.environ.get("DROPIN_CACHE", str(Path.home() / ".cache" / "pcb-dropin")))
    args = parser.parse_args(argv)

    started = time.monotonic()
    # A stop before the router runs leaves nothing to write: leave at once.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    cache = Path(args.cache)
    board = Path(args.board)
    # Negotiation settles with cheap vias (NEGOTIATION_VIA_MM times VIA_FACTOR); via passes then route
    # the nets that have vias again at --via-cost. The export's via cost stays fixed, so its cache holds.
    options = {"pitch": args.pitch, "margin_factor": 0.2, "min_width": args.min_width, "via_cost_mm": NEGOTIATION_VIA_MM}
    log(f"Opening '{args.dsn}'...")
    old_grid, old_meta = None, None
    if args.previous:  # read first, so the edited board is read as a patch of it
        previous_board = Path(args.previous_board) if args.previous_board else Path(args.previous).parent / "prerouted.kicad_pcb"
        if previous_board.exists():
            old_grid, old_meta = export(previous_board_copy(previous_board, board.with_suffix(".kicad_pro"), cache), options, cache)
    grid, meta = export(board, options, cache, old_grid)
    dsn = ses.read_dsn(args.dsn)
    work = cache / f"run-{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "NEGOTIATE_PATIENCE": "10", "NEGOTIATE_WARMUP": "30", "NEGOTIATE_LAZY_VIAS": "1", "NEGOTIATE_TIDY": "1"}
    # A --via-cost below negotiation's own makes negotiation's vias that cheap, and needs no via pass.
    via_factor = min(VIA_FACTOR, args.via_cost / NEGOTIATION_VIA_MM)
    if args.via_cost > NEGOTIATION_VIA_MM * VIA_FACTOR:
        env |= {"NEGOTIATE_VIA_PASS": f"{args.via_cost / NEGOTIATION_VIA_MM:g}", "NEGOTIATE_VIA_PASSES": str(VIA_PASSES)}
    schedule = FULL_SCHEDULE
    plan = None
    if args.previous:
        plan = incremental.plan(incremental.read_session(args.previous), grid, meta, old_grid, old_meta)
        incremental.write_preset(plan.preset, work / "preset.bin")
        env |= {"NEGOTIATE_PRESET": str(work / "preset.bin"), "NEGOTIATE_KEPT_OUT": str(work / "kept.txt"), "NEGOTIATE_KEPT_PRICE": "50"}
        schedule = INCREMENTAL_SCHEDULE
        s = plan.stats
        log(f"Reusing the previous route: {s['pieces_kept']} of {s['pieces']} pieces, {s['nets_fully_kept']} nets whole, "
            f"{s['nets_partly_kept']} in part ({s['pieces_blocked']} pieces blocked by the edit)")
    budget = max(5.0, args.max_seconds - (time.monotonic() - started))
    env["NEGOTIATE_SECONDS"] = f"{budget:.0f}"
    if args.passes > 0:
        env["NEGOTIATE_MAX_ROUNDS"] = str(args.passes)
    dump = work / "run.dump"
    order = ORDERS[args.seed % len(ORDERS)] if args.seed < len(ORDERS) else "random"
    command = [str(ROUTER), str(grid), "x", order, str(args.seed), *schedule, f"{via_factor:g}", str(dump), "12"]
    child = subprocess.Popen(command, stdout=subprocess.PIPE, text=True, env=env)

    def stop(signum: int, _frame) -> None:  # finish the round, write what there is
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # The time limit: the router starts no round after its budget, and is told to stop at the limit
    # whatever it is doing; it then finishes the net in hand and writes what it has.
    deadline = threading.Timer(max(1.0, args.max_seconds - (time.monotonic() - started)), stop, (signal.SIGTERM, None))
    deadline.daemon = True
    deadline.start()
    route_started = time.monotonic()
    summary = ""
    for line in child.stdout:
        line = line.rstrip("\n")
        if line.startswith("Starting auto-routing"):
            log("Starting auto-routing...")
        elif line:
            summary = line
    child.wait()
    deadline.cancel()
    seconds = time.monotonic() - route_started
    if not dump.exists():
        log(f"Auto-routing failed: the router wrote no result ({summary})")
        return 1
    routes = ses.segments(dump, meta)
    kept = {}
    if plan is not None and (work / "kept.txt").exists():
        kept_ids = {int(x) for x in (work / "kept.txt").read_text().split()}
        kept = {code: plan.kept[code] for code in kept_ids if code in plan.kept}
    status = routes["status"]
    names = meta["net_names"]
    if status["incomplete_nets"] or status["conflicted_nets"]:
        log("WARN  not finished: " + ", ".join(
            [f"{names[str(n)]} has no route" for n in status["incomplete_nets"]] + [f"{names[str(n)]} in conflict" for n in status["conflicted_nets"]]))
    if meta.get("sealed"):
        log("Left for the pours (pads inside track keep-outs): " + ", ".join(f"{names.get(c, c)} x{len(p)}" for c, p in meta["sealed"].items()))
    fields = summary.split(",")
    if len(fields) >= 10:
        log(f"Router: {fields[6]} rounds, {fields[8]} vias, {int(fields[9]):,} cells searched")
    log(f"Auto-routing was completed in {seconds:.2f} seconds.")
    log(f"Saving '{args.ses}'...")
    ses.write(routes, meta, dsn, args.ses, kept)
    shutil.rmtree(work, ignore_errors=True)
    return 0 if not (status["incomplete_nets"] or status["conflicted_nets"]) else 2


if __name__ == "__main__":
    sys.exit(main())
