"""One test per item of pcbkit's wrapper contract (INTENT.md, acceptance D).

Blinky (pcbkit's example, copied from a pcbkit checkout) for the quick checks; the spotmicro
carrier, when present in boards/spot, for the time limit and SIGTERM, which need a board that
takes a while. Nothing is written outside a temporary folder.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
BLINKY = Path(os.environ.get("PCBKIT", Path(__file__).resolve().parents[2] / "pcbkit")) / "examples" / "blinky" / "golden"
SPOT = HERE / "boards" / "spot" / "kicad"


def route(tmp: Path, board: Path, dsn: Path, *extra: str, timeout: float = 600) -> tuple[subprocess.CompletedProcess, Path]:
    ses = tmp / "out.ses"
    done = subprocess.run([sys.executable, str(HERE / "route.py"), "-de", str(dsn), "-do", str(ses), "-mp", "40", "--board", str(board),
                           "--cache", str(tmp / "cache"), *extra], capture_output=True, text=True, timeout=timeout)
    return done, ses


def check(tmp: Path, board: Path, ses: Path) -> dict:
    done = subprocess.run([sys.executable, str(HERE / "check.py"), str(board), str(ses), str(tmp / "check")], capture_output=True, text=True)
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.fixture
def blinky(tmp_path: Path) -> tuple[Path, Path]:
    if not BLINKY.exists():
        pytest.skip("no pcbkit checkout")
    shutil.copy(BLINKY / "prerouted.kicad_pcb", tmp_path / "prerouted.kicad_pcb")
    shutil.copy(BLINKY / "blinky.dsn", tmp_path / "blinky.dsn")
    return tmp_path / "prerouted.kicad_pcb", tmp_path / "blinky.dsn"


def test_command_line_writes_a_session_pcbnew_imports_clean(tmp_path: Path, blinky) -> None:
    board, dsn = blinky
    done, ses = route(tmp_path, board, dsn)
    assert ses.exists(), done.stdout + done.stderr
    result = check(tmp_path, board, ses)
    assert result["imported"] and result["copper_problems"] == 0, result


def test_log_marks_the_start_of_routing_before_it_ends(tmp_path: Path, blinky) -> None:
    board, dsn = blinky
    done, _ = route(tmp_path, board, dsn)
    lines = done.stdout.splitlines()
    start = next(i for i, line in enumerate(lines) if "Starting auto-routing" in line)
    end = next(i for i, line in enumerate(lines) if re.search(r"Auto-routing was completed in \d+(\.\d+)? seconds", line))
    saving = next(i for i, line in enumerate(lines) if "Saving '" in line)
    assert start < end < saving


def test_min_width_is_a_floor_for_every_track(tmp_path: Path, blinky) -> None:
    board, dsn = blinky
    done, ses = route(tmp_path, board, dsn, "--min-width", "0.3")
    widths = [int(w) for w in re.findall(r"\(path \S+ (\d+)", ses.read_text())]
    per_mm = 10_000  # resolution um 10
    assert widths and min(widths) >= 0.3 * per_mm, sorted(set(widths))


def test_a_seed_searches_differently(tmp_path: Path, blinky) -> None:
    """The search differs; on a board as small as blinky, tidying may still draw the same track."""
    board, dsn = blinky
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    done_first, _ = route(tmp_path / "a", board, dsn)
    done_second, second = route(tmp_path / "b", board, dsn, "--seed", "1")
    searched = [re.search(r"Router: .*", done.stdout).group(0) for done in (done_first, done_second)]
    assert searched[0] != searched[1]
    assert check(tmp_path / "b", board, second)["copper_problems"] == 0


@pytest.mark.skipif(not SPOT.exists(), reason="the spotmicro carrier is not in boards/spot")
def test_time_limit_stops_and_writes_the_result_so_far(tmp_path: Path) -> None:
    started = time.monotonic()
    done, ses = route(tmp_path, SPOT / "prerouted.kicad_pcb", SPOT / "spotmicro_carrier.dsn", "--max-seconds", "25")
    took = time.monotonic() - started
    assert ses.exists() and "Saving '" in done.stdout
    assert took < 25 + 10, took  # the router stops at the limit, finishing only the net in hand


@pytest.mark.skipif(not SPOT.exists(), reason="the spotmicro carrier is not in boards/spot")
def test_sigterm_writes_the_result_so_far(tmp_path: Path) -> None:
    ses = tmp_path / "out.ses"
    child = subprocess.Popen([sys.executable, str(HERE / "route.py"), "-de", str(SPOT / "spotmicro_carrier.dsn"), "-do", str(ses),
                              "--board", str(SPOT / "prerouted.kicad_pcb"), "--cache", str(tmp_path / "cache")],
                             stdout=subprocess.PIPE, text=True)
    for line in child.stdout:
        if "Starting auto-routing" in line:
            break
    time.sleep(5)
    child.send_signal(signal.SIGTERM)
    sent = time.monotonic()
    rest = child.stdout.read()
    child.wait(timeout=120)
    assert ses.exists() and "Saving '" in rest
    assert time.monotonic() - sent < 5  # pcbkit kills the group 5 s after its SIGTERM
