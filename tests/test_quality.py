"""What the router's finishing steps do on the spotmicro edit: tidying straightens the grid's
staircases, via passes take vias out (or are put back), and on a full and an incremental route
run by route.py, KiCad DRC finds no copper problem.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import HERE, SPOT

import ses  # noqa: E402

pytestmark = pytest.mark.skipif(not (SPOT / "golden").exists(), reason="the spotmicro carrier is not in boards/spot")

FULL = {"NEGOTIATE_PATIENCE": "10", "NEGOTIATE_WARMUP": "30", "NEGOTIATE_MAX_ROUNDS": "40"}


def _route(tmp: Path, grid: Path, name: str, env: dict[str, str]) -> dict:
    dump = tmp / f"{name}.dump"
    subprocess.run([str(HERE / "router" / "negotiate"), str(grid), "x", "span_asc", "0", "0.2", "1.3", "0.3", "1.2", str(dump), "12"],
                   check=True, capture_output=True, timeout=600, env={**os.environ, **FULL, **env})
    return ses.segments(dump, json.loads(grid.with_suffix(".meta.json").read_text())) | {"dump": str(dump)}


@pytest.fixture(scope="module")
def plain(tmp_path_factory, edited) -> dict:
    return _route(tmp_path_factory.mktemp("plain"), edited[0], "plain", {})


def test_tidying_leaves_straight_track_without_a_conflict(plain) -> None:
    """Untidied, the grid's staircases made 6,864 pieces on this route, 5,019 of them under 0.25 mm."""
    assert plain["status"]["conflicted"] == 0
    lengths = [math.hypot(s["x2"] - s["x1"], s["y2"] - s["y1"]) for s in plain["segments"]]
    assert len(lengths) < 2000
    assert sum(length < 0.25 for length in lengths) < len(lengths) / 4


def test_via_passes_take_vias_out_without_a_conflict(tmp_path: Path, edited, plain) -> None:
    fewer = _route(tmp_path, edited[0], "fewer", {"NEGOTIATE_VIA_PASS": "8", "NEGOTIATE_VIA_PASSES": "1"})
    assert fewer["status"]["conflicted"] == 0
    assert fewer["status"]["incomplete"] == plain["status"]["incomplete"]
    assert len(fewer["vias"]) < len(plain["vias"]) * 0.8


def test_a_via_pass_that_cannot_settle_leaves_the_route_as_it_was(tmp_path: Path, edited, plain) -> None:
    """One round cannot settle the conflicts rerouting at dear vias makes, so the pass is put back."""
    back = _route(tmp_path, edited[0], "back", {"NEGOTIATE_VIA_PASS": "8", "NEGOTIATE_VIA_PASSES": "1", "NEGOTIATE_VIA_PASS_ROUNDS": "1"})
    assert (tmp_path / "back.dump").read_bytes() == (Path(plain["dump"])).read_bytes()


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_route_py_defaults_give_a_drc_clean_session(tmp_path: Path, incremental: bool) -> None:
    """The settings route.py passes, together: lazy vias, two via passes and tidying."""
    kicad = SPOT / "kicad"
    previous = ["--previous", str(SPOT / "golden" / "spotmicro_carrier.ses"),
                "--previous-board", str(SPOT / "golden" / "prerouted.kicad_pcb")] if incremental else []
    out = tmp_path / "out.ses"
    done = subprocess.run([sys.executable, str(HERE / "route.py"), "-de", str(kicad / "spotmicro_carrier.dsn"), "-do", str(out),
                           "-mp", "40", "--board", str(kicad / "prerouted.kicad_pcb"), "--cache", str(tmp_path / "cache"), *previous],
                          capture_output=True, text=True, timeout=900)
    assert done.returncode == 0, done.stdout + done.stderr
    checked = subprocess.run([sys.executable, str(HERE / "check.py"), str(kicad / "prerouted.kicad_pcb"), str(out), str(tmp_path / "check")],
                             capture_output=True, text=True)
    result = json.loads(checked.stdout.strip().splitlines()[-1])
    assert result["imported"] and result["copper_problems"] == 0, result
