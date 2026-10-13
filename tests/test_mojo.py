"""With its improvements off (the NEGOTIATE_* settings route.py adds), the Mojo router must write
what the C++ reference router (negotiate6.cpp) writes, byte for byte.

On the spotmicro edit: an incremental run (the previous route preset, as route.py runs it) and a
short full route with a non-zero seed, which takes a different net order and jitters the prices.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import HERE, OPTIONS, SPOT

pytestmark = pytest.mark.skipif(not (SPOT / "golden").exists(), reason="the spotmicro carrier is not in boards/spot")


def _both(tmp: Path, grid: Path, args: list[str], env: dict[str, str]) -> tuple[bytes, bytes]:
    dumps = []
    for router in (HERE / "negotiate6", HERE / "mojo" / "negotiate"):
        dump = tmp / f"{router.name}.dump"
        kept = tmp / f"{router.name}.kept"
        subprocess.run([str(router), str(grid), "x", *args, str(dump), "12"], check=True, capture_output=True, timeout=600,
                       env={**os.environ, **env, "NEGOTIATE_KEPT_OUT": str(kept)})
        dumps.append(dump.read_bytes() + b"|" + kept.read_bytes())
    return dumps[0], dumps[1]


def test_an_incremental_run_matches(tmp_path: Path, edited) -> None:
    grid, preset = edited
    env = {"NEGOTIATE_PRESET": str(preset), "NEGOTIATE_KEPT_PRICE": "50", "NEGOTIATE_PATIENCE": "10", "NEGOTIATE_WARMUP": "30",
           "NEGOTIATE_SECONDS": "600", "NEGOTIATE_MAX_ROUNDS": "40"}
    reference, mojo = _both(tmp_path, grid, ["span_asc", "0", "0.5", "1.6", "0.5", "1.2"], env)
    assert reference == mojo


def test_a_seeded_full_route_matches(tmp_path: Path, edited) -> None:
    grid, _ = edited
    reference, mojo = _both(tmp_path, grid, ["pads_desc", "1", "0.2", "1.3", "0.3", "1.2"], {"NEGOTIATE_MAX_ROUNDS": "2"})
    assert reference == mojo


def test_a_four_layer_board_matches(tmp_path: Path) -> None:
    """Vias to every other layer: a 4-layer board offers more steps per cell than a 2-layer one."""
    blinky = Path(os.environ.get("PCBKIT", Path(__file__).resolve().parents[2] / "pcbkit")) / "examples" / "blinky" / "golden" / "prerouted.kicad_pcb"
    if not blinky.exists():
        pytest.skip("no pcbkit checkout")
    board = tmp_path / "prerouted.kicad_pcb"
    script = (f"import pcbnew; b = pcbnew.LoadBoard({str(blinky)!r}); b.SetCopperLayerCount(4); "
              f"pcbnew.SaveBoard({str(board)!r}, b)")
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True)
    options = tmp_path / "options.json"
    options.write_text(json.dumps(OPTIONS))
    grid = tmp_path / "four.board"
    subprocess.run([sys.executable, str(HERE / "board.py"), "export", str(board), str(grid), str(grid.with_suffix(".meta.json")),
                    str(options)], check=True, capture_output=True)
    assert json.loads(grid.with_suffix(".meta.json").read_text())["shape"][0] == 4
    reference, mojo = _both(tmp_path, grid, ["span_asc", "0", "0.2", "1.3", "0.3", "1.2"], {})
    assert reference == mojo


def test_a_negative_seed_matches(tmp_path: Path, edited) -> None:
    """route.py passes --seed through; C++ reads it as unsigned, so -1 still jitters the prices."""
    grid, _ = edited
    reference, mojo = _both(tmp_path, grid, ["area_desc", "-1", "0.2", "1.3", "0.3", "1.2"], {"NEGOTIATE_MAX_ROUNDS": "1"})
    assert reference == mojo
