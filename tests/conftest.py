"""Shared fixtures: the spotmicro carrier's edit, exported for the router."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import incremental  # noqa: E402

SPOT = HERE / "boards" / "spot"
OPTIONS = {"pitch": 0.05, "margin_factor": 0.2, "min_width": 0.0, "via_cost_mm": 2.0}


@pytest.fixture(scope="session")
def edited(tmp_path_factory) -> tuple[Path, Path]:
    """The edited board's grid, and the previous route's preset for it."""
    tmp = tmp_path_factory.mktemp("spot")
    options = tmp / "options.json"
    options.write_text(json.dumps(OPTIONS))
    grids = {}
    for name, board in (("golden", SPOT / "golden" / "prerouted.kicad_pcb"), ("kicad", SPOT / "kicad" / "prerouted.kicad_pcb")):
        out = tmp / f"{name}.board"
        previous = [str(grids["golden"])] if grids else []
        subprocess.run([sys.executable, str(HERE / "board.py"), "export", str(board), str(out), str(out.with_suffix(".meta.json")),
                        str(options), *previous], check=True, capture_output=True)
        grids[name] = out
    plan = incremental.plan(incremental.read_session(SPOT / "golden" / "spotmicro_carrier.ses"), grids["kicad"],
                            json.loads(grids["kicad"].with_suffix(".meta.json").read_text()), grids["golden"],
                            json.loads(grids["golden"].with_suffix(".meta.json").read_text()))
    incremental.write_preset(plan.preset, tmp / "preset.bin")
    return grids["kicad"], tmp / "preset.bin"


