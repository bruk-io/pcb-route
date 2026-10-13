"""Import a session into a board with pcbnew and count KiCad DRC's copper problems.

The copper categories are pcbkit's (route/freerouting.py COPPER_CATEGORIES). The board's project
file is copied next to the saved board so DRC sees the same net classes and rules.

Usage: python check.py <board.kicad_pcb> <session.ses> <out dir>   -> one JSON line
"""

from __future__ import annotations

import collections
import json
import shutil
import subprocess
import sys
from pathlib import Path

COPPER_CATEGORIES = ("unconnected_items", "clearance", "shorting_items", "tracks_crossing", "hole_clearance", "copper_edge_clearance")


def check(board_path: str, ses_path: str, out_dir: str) -> dict:
    import pcbnew

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    board = pcbnew.LoadBoard(board_path)
    imported = pcbnew.ImportSpecctraSES(board, ses_path)
    routed = out / "routed.kicad_pcb"
    pcbnew.SaveBoard(str(routed), board)
    project = Path(board_path).with_suffix(".kicad_pro")
    if project.exists():
        shutil.copy(project, out / "routed.kicad_pro")
    report = out / "drc.json"
    subprocess.run(["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all", "-o", str(report), str(routed)], capture_output=True)
    drc = json.loads(report.read_text())
    counts = collections.Counter(v["type"] for v in drc.get("violations", []))
    unconnected = len(drc.get("unconnected_items", []))
    copper = {k: counts.get(k, 0) for k in COPPER_CATEGORIES if k != "unconnected_items"} | {"unconnected_items": unconnected}
    tracks = [t for t in board.GetTracks()]
    # Vias the session added that sit inside a surface-mount pad (an assembly risk DRC does not count).
    before = pcbnew.LoadBoard(board_path)
    old_vias = {(v.GetPosition().x, v.GetPosition().y) for v in before.GetTracks() if v.Type() == pcbnew.PCB_VIA_T}
    smd = [p for p in board.GetPads() if p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD]
    in_pad = 0
    for v in tracks:
        if v.Type() == pcbnew.PCB_VIA_T and (v.GetPosition().x, v.GetPosition().y) not in old_vias:
            in_pad += any(p.HitTest(v.GetPosition()) for p in smd)
    return {"imported": bool(imported), "copper": copper, "copper_problems": sum(copper.values()),
            "tracks": sum(t.Type() != pcbnew.PCB_VIA_T for t in tracks), "vias": sum(t.Type() == pcbnew.PCB_VIA_T for t in tracks),
            "vias_in_smd_pads": in_pad, "other": {k: v for k, v in counts.items() if k not in COPPER_CATEGORIES and not k.startswith("silk")}}


if __name__ == "__main__":
    print(json.dumps(check(*sys.argv[1:4])))
