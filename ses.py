"""Turn the router's result into a Specctra session (SES) file that pcbnew imports.

The router's dump holds, per net, the grid cells of every connection it made; ``segments`` turns
those into straight track pieces and vias in millimetres (the research harness's routes.py, kept
here so the drop-in stands alone). ``write`` writes them as a session: wires per net on the DSN's
layer names, vias named after the DSN's via padstacks, coordinates in the DSN's resolution with
the y axis flipped, as Specctra has it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

UNIT_MM = {"um": 0.001, "mm": 1.0, "mil": 0.0254, "inch": 25.4}


@dataclass(frozen=True)
class Dsn:
    """What a session needs from the design file."""

    name: str
    unit: str
    per_unit: int  # resolution: steps per unit
    via_padstacks: dict[str, str]  # padstack name -> its text block, for the session's library_out

    def scale(self) -> float:
        """Session steps per millimetre."""
        return self.per_unit / UNIT_MM[self.unit]


def read_dsn(path: str | Path) -> Dsn:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    name = re.search(r"\(pcb\s+(\"[^\"]*\"|\S+)", text).group(1).strip('"')
    name = Path(name).stem
    resolution = re.search(r"\(resolution\s+(\w+)\s+(\d+)\)", text)
    unit, per_unit = (resolution.group(1), int(resolution.group(2))) if resolution else ("um", 10)
    padstacks: dict[str, str] = {}
    for match in re.finditer(r"\(padstack\s+(\"[^\"]*\"|\S+)", text):
        padstack = match.group(1).strip('"')
        if not padstack.startswith("Via"):
            continue
        start, depth, i = match.start(), 0, match.start()
        while True:  # the padstack's whole bracketed block
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        padstacks[padstack] = text[start : i + 1]
    return Dsn(name, unit, per_unit, padstacks)


def segments(dump_path: str | Path, meta: dict) -> dict:
    """Track pieces and vias, in millimetres, from the router's dump."""
    layers, rows, cols = meta["shape"]
    plane = rows * cols
    pitch, x0, y0 = meta["pitch"], meta["x0"], meta["y0"]

    def place(cell: int) -> tuple[float, float]:
        in_plane = cell % plane
        return (round(x0 + (in_plane % cols + 0.5) * pitch, 6), round(y0 + (in_plane // cols + 0.5) * pitch, 6))

    def piece(net: int, rules: dict, start: int, end: int) -> dict:
        (x1, y1), (x2, y2) = place(start), place(end)
        return {"net": net, "layer": start // plane, "width": rules["width"], "x1": x1, "y1": y1, "x2": x2, "y2": y2}

    raw = Path(dump_path).read_bytes()
    count, offset = int(np.frombuffer(raw, np.int32, 1)[0]), 4
    pieces: list[dict] = []
    vias: list[dict] = []
    status = {"nets": count, "incomplete": 0, "conflicted": 0, "incomplete_nets": [], "conflicted_nets": []}
    for _ in range(count):
        net, dead, conflicted, connection_count = (int(v) for v in np.frombuffer(raw, np.int32, 4, offset))
        offset += 16
        status["incomplete"] += dead
        status["conflicted"] += conflicted
        if dead:
            status["incomplete_nets"].append(net)
        if conflicted:
            status["conflicted_nets"].append(net)
        info = meta["nets"][str(net)]
        rules = meta["classes"][info["class"]]
        connections = []
        for _ in range(connection_count):
            size = int(np.frombuffer(raw, np.int32, 1, offset)[0])
            offset += 4
            connections.append(np.frombuffer(raw, np.uint32, size, offset).astype(np.int64).tolist())
            offset += 4 * size
        # Where one connection starts or ends on another, the track must break there so the
        # junction is an endpoint on both.
        joints = {end for walked in connections for end in (walked[0], walked[-1])}
        loose = {}  # cells standing in for a pad too small to cover any cell centre
        for pad in info["pads"]:
            if not pad["exact"]:
                row = min(rows - 1, max(0, int((pad["y"] - y0) / pitch)))
                col = min(cols - 1, max(0, int((pad["x"] - x0) / pitch)))
                loose[row * cols + col] = (pad["x"], pad["y"])
        for walked in connections:
            for end in (walked[0], walked[-1]):
                if end % plane in loose:
                    (ax, ay), (px, py) = place(end), loose[end % plane]
                    pieces.append({"net": net, "layer": end // plane, "width": rules["width"], "x1": ax, "y1": ay, "x2": px, "y2": py})
            run_start = walked[0]
            for i in range(1, len(walked)):
                before, here = walked[i - 1], walked[i]
                if before % plane == here % plane:  # a layer change
                    if run_start != before:
                        pieces.append(piece(net, rules, run_start, before))
                    x, y = place(here)
                    vias.append({"net": net, "x": x, "y": y, "diameter": rules["via_diameter"], "drill": rules["via_drill"]})
                    run_start = here
                    continue
                turning = i + 1 < len(walked) and (walked[i + 1] % plane == here % plane or walked[i + 1] - here != here - before)
                if turning or here in joints or i + 1 == len(walked):
                    pieces.append(piece(net, rules, run_start, here))
                    run_start = here
    return {"segments": pieces, "vias": vias, "status": status}


def _quote(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9_/+.\-\[\]:]+", name) else '"' + name.replace('"', "'") + '"'


def _via_name(dsn: Dsn, diameter: float, drill: float) -> str:
    wanted = f"Via[0-1]_{round(diameter * 1000)}:{round(drill * 1000)}_um"
    if wanted in dsn.via_padstacks:
        return wanted
    # nearest diameter among the DSN's vias
    sizes = {name: float(m.group(1)) for name in dsn.via_padstacks if (m := re.search(r"_(\d+(?:\.\d+)?):", name))}
    return min(sizes, key=lambda name: abs(sizes[name] - diameter * 1000)) if sizes else wanted


def write(routes: dict, meta: dict, dsn: Dsn, out_path: str | Path, kept: dict | None = None) -> None:
    """Write `routes` (from `segments`) as a session file for `dsn`.

    `kept` maps net code to copper reused from a previous session (incremental.OldNet: wires as
    layer name, width and points in mm; vias as padstack name and position), written as it was.
    """
    scale = dsn.scale()
    to = lambda mm: f"{round(mm * scale)}"  # noqa: E731
    by_net: dict[int, list[str]] = {}
    used_vias: set[str] = set()
    for s in routes["segments"]:
        layer = meta["layer_names"][s["layer"]]
        by_net.setdefault(s["net"], []).append(
            f"        (wire\n          (path {layer} {to(s['width'])}\n            {to(s['x1'])} {to(-s['y1'])}\n            {to(s['x2'])} {to(-s['y2'])}\n          )\n        )"
        )
    for v in routes["vias"]:
        name = _via_name(dsn, v["diameter"], v["drill"])
        used_vias.add(name)
        by_net.setdefault(v["net"], []).append(f'        (via "{name}" {to(v["x"])} {to(-v["y"])}\n        )')
    for net, old in (kept or {}).items():
        for layer, width, points in old.wires:
            coords = "\n".join(f"            {to(x)} {to(-y)}" for x, y in points)
            by_net.setdefault(net, []).append(f"        (wire\n          (path {layer} {to(width)}\n{coords}\n          )\n        )")
        for padstack, x, y in old.vias:
            used_vias.add(padstack)
            by_net.setdefault(net, []).append(f'        (via "{padstack}" {to(x)} {to(-y)}\n        )')
    lines = [f"(session {_quote(dsn.name)}", f"  (base_design {_quote(dsn.name)})", "  (was_is\n  )", "  (routes ", f"    (resolution {dsn.unit} {dsn.per_unit})"]
    lines += ['    (parser\n      (host_cad "KiCad\'s Pcbnew")\n      (host_version "dropin")\n    )']
    lines.append("    (library_out ")
    sizes = {v["diameter"] for v in routes["vias"]}
    for name in sorted(used_vias):
        match = re.search(r"_(\d+(?:\.\d+)?):", name)
        diameter = float(match.group(1)) / 1000 if match else max(sizes, default=0.6)
        shapes = "".join(f"\n        (shape\n          (circle {layer} {to(diameter)} 0 0)\n        )" for layer in meta["layer_names"])
        lines.append(f'      (padstack "{name}"{shapes}\n        (attach off)\n      )')
    lines.append("    )")
    lines.append("    (network_out ")
    for net in sorted(by_net):
        lines.append(f"      (net {_quote(meta['net_names'][str(net)])}")
        lines += by_net[net]
        lines.append("      )")
    lines.append("    )\n  )\n)")
    Path(out_path).write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    import sys

    dump, meta_path, dsn_path, out = sys.argv[1:5]
    meta = json.loads(Path(meta_path).read_text())
    write(segments(dump, meta), meta, read_dsn(dsn_path), out)
