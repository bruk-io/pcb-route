"""A patched export must be the same, byte for byte, as reading the edited board from scratch.

Each test edits a copy of the spotmicro carrier with pcbnew the way a designer would (move,
rotate, delete, re-net, add a hand-laid track or a keep-out), then exports it twice: in full, and
patched from the unedited board's export. Saving through pcbnew also renumbers net codes, which the
patch has to translate.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parents[1]
SPOT = HERE / "boards" / "spot" / "kicad"
OPTIONS = {"pitch": 0.05, "margin_factor": 0.2, "min_width": 0.0, "via_cost_mm": 2.0}

pytestmark = pytest.mark.skipif(not SPOT.exists(), reason="the spotmicro carrier is not in boards/spot")


def _export(board: Path, out: Path, previous: Path | None = None) -> str:
    options = out.with_suffix(".options.json")
    options.write_text(json.dumps(OPTIONS))
    command = [sys.executable, str(HERE / "board.py"), "export", str(board), str(out), str(out.with_suffix(".meta.json")), str(options)]
    done = subprocess.run(command + ([str(previous)] if previous else []), capture_output=True, text=True, check=True)
    return next(line for line in done.stderr.splitlines() if line.startswith("export:"))


def _edited(tmp: Path, name: str, *edits: str) -> Path:
    """A copy of the board with the named edits made, each run in a fresh process: pcbnew keeps
    state between boards loaded in one process, and a removed footprint can break the next load."""
    folder = tmp / name
    folder.mkdir()
    path = folder / "prerouted.kicad_pcb"
    script = (f"import sys; sys.path.insert(0, {str(Path(__file__).parent)!r}); import pcbnew, test_patch as t; "
              f"b = pcbnew.LoadBoard({str(SPOT / 'prerouted.kicad_pcb')!r}); "
              f"[t.EDITS[e](pcbnew, b) for e in {list(edits)!r}]; pcbnew.SaveBoard({str(path)!r}, b)")
    subprocess.run([sys.executable, "-c", script], capture_output=True, check=True)
    shutil.copy(SPOT / "prerouted.kicad_pro", folder / "prerouted.kicad_pro")
    return path


def _footprint(board, reference: str):
    return next(f for f in board.GetFootprints() if f.GetReference() == reference)


def _move(pcbnew, board) -> None:
    _footprint(board, "C14").Move(pcbnew.VECTOR2I(pcbnew.FromMM(2.5), pcbnew.FromMM(-1.0)))


def _rotate(pcbnew, board) -> None:
    _footprint(board, "R70").Rotate(_footprint(board, "R70").GetPosition(), pcbnew.EDA_ANGLE(90, pcbnew.DEGREES_T))


def _delete(pcbnew, board) -> None:
    board.Remove(_footprint(board, "R84"))


def _renet(pcbnew, board) -> None:
    pads = list(_footprint(board, "R6").Pads())
    other = next(p for p in _footprint(board, "R41").Pads() if p.GetNetCode() > 0)
    pads[0].SetNet(other.GetNet())


def _track(pcbnew, board) -> None:
    pad = next(p for p in _footprint(board, "R52").Pads() if p.GetNetCode() > 0)
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(pad.GetPosition())
    track.SetEnd(pad.GetPosition() + pcbnew.VECTOR2I(pcbnew.FromMM(3.0), 0))
    track.SetWidth(pcbnew.FromMM(0.25))
    track.SetLayer(pcbnew.F_Cu)
    track.SetNet(pad.GetNet())
    board.Add(track)


def _keepout(pcbnew, board) -> None:
    zone = pcbnew.ZONE(board)
    zone.SetIsRuleArea(True)
    zone.SetDoNotAllowTracks(True)
    zone.SetDoNotAllowVias(True)
    zone.SetLayerSet(pcbnew.LSET.AllCuMask())
    outline = zone.Outline()
    outline.NewOutline()
    for x, y in ((100, 80), (106, 80), (106, 84), (100, 84)):
        outline.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
    board.Add(zone)


def _untrack(pcbnew, board) -> None:
    board.Remove(next(t for t in board.GetTracks() if t.Type() == pcbnew.PCB_TRACE_T))


EDITS = {"move": _move, "rotate": _rotate, "delete": _delete, "renet": _renet, "track": _track, "keepout": _keepout,
         "untrack": _untrack}


@pytest.fixture(scope="module")
def base(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("base") / "base.board"
    _export(SPOT / "prerouted.kicad_pcb", out)
    return out


def _same(a: Path, b: Path) -> None:
    for suffix in ("", ".state.json", ".state.npz"):
        assert Path(str(a) + suffix).read_bytes() == Path(str(b) + suffix).read_bytes(), f"{a.name}{suffix} differs"
    assert a.with_suffix(".meta.json").read_text() == b.with_suffix(".meta.json").read_text()


@pytest.mark.parametrize("name", EDITS)
def test_a_patch_is_the_full_export(tmp_path: Path, base: Path, name: str) -> None:
    board = _edited(tmp_path, name, name)
    full, patched = tmp_path / "full.board", tmp_path / "patched.board"
    _export(board, full)
    how = _export(board, patched, previous=base)
    assert "patch" in how, how
    _same(full, patched)


def test_patches_chain(tmp_path: Path, base: Path) -> None:
    first = _edited(tmp_path, "first", "move")
    second = _edited(tmp_path, "second", "move", "rotate")
    step, full, patched = tmp_path / "step.board", tmp_path / "full.board", tmp_path / "patched.board"
    _export(first, step, previous=base)
    _export(second, full)
    assert "patch" in _export(second, patched, previous=step)
    _same(full, patched)


def test_the_via_sites_follow_from_the_maps(tmp_path: Path, base: Path) -> None:
    """The exporter works out where each net may drill (and patches it after an edit); worked out
    again here from its plain via map and the owner map, by the rule the router relies on, it must
    agree: a via needs its whole blob, on every layer, to be enterable by the one net."""
    board = _edited(tmp_path, "move", "move")
    patched = tmp_path / "patched.board"
    _export(board, patched, previous=base)
    for cls, (exported, recomputed) in enumerate(_via_sites(patched)):
        assert np.array_equal(exported, recomputed), f"class {cls}: {int((exported != recomputed).sum())} cells differ"


def _via_sites(path: Path):
    """Per class, the board file's via sites and the same worked out from its other maps."""
    free, nobody = -1, -2
    raw = path.read_bytes()
    layers, rows, cols, k, _, _ = np.frombuffer(raw, np.int32, 6)
    offset = 24
    blob = np.frombuffer(raw, np.int32, k, offset) / 1000.0
    offset += 4 * k + 4 * k * k
    plane, n = rows * cols, rows * cols * layers

    def read(count: int) -> np.ndarray:
        nonlocal offset
        values = np.frombuffer(raw, np.int16, count, offset)
        offset += 2 * count
        return values

    trace = [read(n) for _ in range(k)]
    plain = [read(plane) for _ in range(k)]
    core = [read(n) for _ in range(k)]
    read(n)  # the pad map
    sites = [read(plane) for _ in range(k)]
    for cls in range(k):
        owner = np.where((trace[cls] == nobody) & (core[cls] >= 0), core[cls], trace[cls]).reshape(layers, rows, cols)
        who = plain[cls].reshape(rows, cols).copy()
        reach = int(np.floor(blob[cls]))
        for dr in range(-reach, reach + 1):
            for dc in range(-reach, reach + 1):
                if dr * dr + dc * dc > blob[cls] ** 2:
                    continue
                for layer in range(layers):
                    here = np.full((rows, cols), nobody, np.int16)  # past the edge: no via
                    here[max(0, -dr):rows - max(0, dr), max(0, -dc):cols - max(0, dc)] = \
                        owner[layer, max(0, dr):rows + min(0, dr), max(0, dc):cols + min(0, dc)]
                    joins = ((who == free) | (who == here)) & (here != nobody)
                    who = np.where(here == free, who, np.where(joins, here, nobody)).astype(np.int16)
        yield sites[cls].reshape(rows, cols), who
