"""The router's output, pinned. Each case runs the router and compares a fingerprint (SHA-256) of
what it wrote, the dump and the kept list, with the one in snapshots.json.

A change to the router that changes its output fails here. That may be the point of the change:
then check the new output is better (test_quality.py, check.py), and accept it with

    UPDATE_SNAPSHOTS=1 python -m pytest tests/test_snapshots.py

The router is compiled for the machine it runs on; a CPU without fused multiply-add may round
some prices differently and so route differently. The fingerprints were taken on x86-64 with it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import HERE, OPTIONS, SPOT

SNAPSHOTS = Path(__file__).with_name("snapshots.json")
UPDATE = bool(os.environ.get("UPDATE_SNAPSHOTS"))
BLINKY = Path(os.environ.get("PCBKIT", Path(__file__).resolve().parents[2] / "pcbkit")) / "examples" / "blinky" / "golden"
SETTLE = {"NEGOTIATE_PATIENCE": "10", "NEGOTIATE_WARMUP": "30", "NEGOTIATE_MAX_ROUNDS": "40"}

needs_spot = pytest.mark.skipif(not (SPOT / "golden").exists(), reason="the spotmicro carrier is not in boards/spot")


def _matches(name: str, tmp: Path, grid: Path, args: list[str], env: dict[str, str]) -> None:
    dump, kept = tmp / "out.dump", tmp / "kept.txt"
    subprocess.run([str(HERE / "router" / "negotiate"), str(grid), "x", *args, str(dump), "12"], check=True, capture_output=True,
                   timeout=600, env={**os.environ, **env, "NEGOTIATE_KEPT_OUT": str(kept)})
    found = hashlib.sha256(dump.read_bytes() + b"|" + (kept.read_bytes() if kept.exists() else b"")).hexdigest()
    pinned = json.loads(SNAPSHOTS.read_text()) if SNAPSHOTS.exists() else {}
    if UPDATE:
        pinned[name] = found
        SNAPSHOTS.write_text(json.dumps(pinned, indent=2, sort_keys=True) + "\n")
        return
    assert pinned.get(name) == found, (
        f"the router's output for {name!r} changed. If that is intended, check the new route is better "
        "and accept it with UPDATE_SNAPSHOTS=1 python -m pytest tests/test_snapshots.py")


@needs_spot
def test_an_incremental_run(tmp_path: Path, edited) -> None:
    """As route.py runs it after an edit: the previous route preset, then via passes and tidying."""
    grid, preset = edited
    env = SETTLE | {"NEGOTIATE_PRESET": str(preset), "NEGOTIATE_KEPT_PRICE": "50", "NEGOTIATE_VIA_PASS": "8", "NEGOTIATE_VIA_PASSES": "2"}
    _matches("incremental", tmp_path, grid, ["span_asc", "0", "0.5", "1.6", "0.5", "1.2"], env)


@needs_spot
def test_a_seeded_full_route_cut_short(tmp_path: Path, edited) -> None:
    """Another net order and jittered prices; two rounds leave conflicts, which tidying leaves alone."""
    _matches("seeded", tmp_path, edited[0], ["pads_desc", "1", "0.2", "1.3", "0.3", "1.2"], {"NEGOTIATE_MAX_ROUNDS": "2"})


@needs_spot
def test_a_negative_seed(tmp_path: Path, edited) -> None:
    """route.py passes --seed through; it is read as unsigned, so -1 still jitters the prices."""
    _matches("negative_seed", tmp_path, edited[0], ["area_desc", "-1", "0.2", "1.3", "0.3", "1.2"], {"NEGOTIATE_MAX_ROUNDS": "1"})


def test_a_four_layer_board(tmp_path: Path) -> None:
    """Vias to every other layer: a 4-layer board offers more steps per cell than a 2-layer one."""
    if not BLINKY.exists():
        pytest.skip("no pcbkit checkout")
    board = tmp_path / "prerouted.kicad_pcb"
    script = (f"import pcbnew; b = pcbnew.LoadBoard({str(BLINKY / 'prerouted.kicad_pcb')!r}); b.SetCopperLayerCount(4); "
              f"pcbnew.SaveBoard({str(board)!r}, b)")
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True)
    options = tmp_path / "options.json"
    options.write_text(json.dumps(OPTIONS))
    grid = tmp_path / "four.board"
    subprocess.run([sys.executable, str(HERE / "board.py"), "export", str(board), str(grid), str(grid.with_suffix(".meta.json")),
                    str(options)], check=True, capture_output=True)
    assert json.loads(grid.with_suffix(".meta.json").read_text())["shape"][0] == 4
    _matches("four_layers", tmp_path, grid, ["span_asc", "0", "0.2", "1.3", "0.3", "1.2"], SETTLE | {"NEGOTIATE_VIA_PASS": "8"})
