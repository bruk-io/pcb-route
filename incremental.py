"""Reuse a previous session's route on an edited board.

For each net of the edited board, the previous session's wires and vias of the same net name are
put on the new grid. A piece is dropped when any of its cells is now closed to that net (a moved
pad, a new hand-laid stub or keep-out claims it). Of the pieces left, the connected group that
touches the most of the net's terminals is kept; the rest are dropped. The router lays the kept
group without searching and connects whatever terminals it does not reach. Nothing is locked: if a
kept group ends up in conflict, the router rips it up and routes that net afresh.

The session writer then writes kept groups as they were in the previous session, plus the new
connections.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

FREE, NOBODY = -1, -2
UNIT_MM = {"um": 0.001, "mm": 1.0, "mil": 0.0254, "inch": 25.4}


# --- the previous session --------------------------------------------------------------------


def _tokens(text: str) -> list:
    """A Specctra file as nested lists of strings."""
    stack: list[list] = [[]]
    for token in re.findall(r'\(|\)|"[^"]*"|[^\s()]+', text):
        if token == "(":
            stack.append([])
        elif token == ")":
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(token.strip('"'))
    return stack[0]


def _find(node: list, name: str) -> list[list]:
    found = []
    for child in node:
        if isinstance(child, list) and child:
            if child[0] == name:
                found.append(child)
            found += _find(child, name)
    return found


@dataclass
class OldNet:
    wires: list[tuple[str, float, list[tuple[float, float]]]] = field(default_factory=list)  # layer, width mm, points mm
    vias: list[tuple[str, float, float]] = field(default_factory=list)  # padstack, x mm, y mm


def read_session(path: str | Path) -> dict[str, OldNet]:
    """Wires and vias per net name, in KiCad's millimetres (y down)."""
    tree = _tokens(Path(path).read_text(encoding="utf-8", errors="replace"))
    routes = _find(tree, "routes")[0]
    resolution = _find(routes, "resolution")[0]
    per_mm = int(resolution[2]) / UNIT_MM[resolution[1]]
    nets: dict[str, OldNet] = {}
    for net in _find(_find(routes, "network_out")[0], "net"):
        old = nets.setdefault(net[1], OldNet())
        for wire in (c for c in net[2:] if isinstance(c, list) and c and c[0] == "wire"):
            path = next(c for c in wire[1:] if isinstance(c, list) and c and c[0] == "path")
            numbers = [float(v) for v in path[3:] if not isinstance(v, list)]
            points = [(numbers[i] / per_mm, -numbers[i + 1] / per_mm) for i in range(0, len(numbers) - 1, 2)]
            old.wires.append((path[1], float(path[2]) / per_mm, points))
        for via in (c for c in net[2:] if isinstance(c, list) and c and c[0] == "via"):
            old.vias.append((via[1], float(via[2]) / per_mm, -float(via[3]) / per_mm))
    return nets


# --- the new board's maps --------------------------------------------------------------------


@dataclass(frozen=True)
class Maps:
    """One export's maps, mapped from its file rather than read: a plan looks at few of the cells."""

    trace: list[np.ndarray]
    core: list[np.ndarray]
    via: list[np.ndarray]

    def owner(self, cls: int, cells: np.ndarray) -> np.ndarray:
        """Which net alone may enter each cell, as the router works it out."""
        t, c = self.trace[cls][cells], self.core[cls][cells]
        return np.where((t == NOBODY) & (c >= 0), c, t)


def read_maps(board_path: str | Path, meta: dict) -> tuple[Maps, dict[int, list[np.ndarray]]]:
    """Per class the trace, core and via maps (mapped, not read); terminals per net."""
    layers, rows, cols = meta["shape"]
    k, n, plane = len(meta["classes"]), layers * rows * cols, rows * cols
    path = str(board_path)
    net_count = int(np.fromfile(path, np.int32, 6)[4])
    offset = 4 * (6 + k + k * k)

    def take(count: int, size: int) -> list[np.ndarray]:
        nonlocal offset
        maps = []
        for _ in range(count):
            maps.append(np.memmap(path, np.int16, "r", offset, (size,)))
            offset += 2 * size
        return maps

    trace, via, core = take(k, n), take(k, plane), take(k, n)
    offset += 2 * n + 2 * plane * k  # the pad map, the via-site maps
    with open(path, "rb") as source:
        source.seek(offset)
        raw = source.read()
    at = 0
    terminals: dict[int, list[np.ndarray]] = {}
    for _ in range(net_count):
        code, _cls, count = (int(v) for v in np.frombuffer(raw, np.int32, 3, at))
        at += 12
        cells = []
        for _ in range(count):
            size = int(np.frombuffer(raw, np.int32, 1, at)[0])
            at += 4
            cells.append(np.frombuffer(raw, np.uint32, size, at).astype(np.int64))
            at += 4 * size
        terminals[code] = cells
    return Maps(trace, core, via), terminals


# --- matching old pieces to the new board ----------------------------------------------------


@dataclass
class Plan:
    """What to reuse: per net code, the cells and via positions to preset, and the old pieces kept."""

    preset: dict[int, tuple[np.ndarray, np.ndarray]]
    kept: dict[int, OldNet]
    stats: dict


def plan(old: dict[str, OldNet], board_path: str | Path, meta: dict, old_board: str | Path | None = None, old_meta: dict | None = None) -> Plan:
    """Decide what of the previous route to reuse.

    With the previous board's export on the same grid, a piece is dropped only where the edit
    changed what may stand in its cells (a cell now closed to the net whose owner differs from
    before). Without it, a piece is dropped where any cell is closed, which is stricter: the
    raster's rounding margin also closes some cells a legal track passes through.
    """
    layers, rows, cols = meta["shape"]
    plane = rows * cols
    x0, y0, pitch = meta["x0"], meta["y0"], meta["pitch"]
    maps, terminals = read_maps(board_path, meta)
    same_grid = old_board is not None and old_meta is not None and all(old_meta[k] == meta[k] for k in ("shape", "x0", "y0", "pitch"))
    old_maps, table = None, None
    if same_grid:
        old_maps, _ = read_maps(old_board, old_meta)
        # Net codes are renumbered between boards: translate old values into the new codes by name.
        new_code = {name: int(code) for code, name in meta.get("all_net_names", meta["net_names"]).items()}
        table = np.arange(-2, 32768, dtype=np.int32)  # index value + 2 -> translated value
        for code, name in old_meta.get("all_net_names", old_meta["net_names"]).items():
            table[int(code) + 2] = new_code.get(name, -3)  # -3: a net the new board does not have
        old_class_of = {name: old_meta["nets"][code]["class"] for code, name in old_meta["net_names"].items()}
    code_of = {name: int(code) for code, name in meta["net_names"].items()}
    layer_of = {name: i for i, name in enumerate(meta["layer_names"])}

    def cell(x: float, y: float) -> int | None:
        r, c = int((y - y0) / pitch), int((x - x0) / pitch)
        return r * cols + c if 0 <= r < rows and 0 <= c < cols else None

    preset: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    kept: dict[int, OldNet] = {}
    stats = {"nets_in_session": len(old), "nets_matched": 0, "pieces": 0, "pieces_blocked": 0, "pieces_kept": 0,
             "nets_fully_kept": 0, "nets_partly_kept": 0, "nets_not_kept": 0, "nets_rules_changed": 0, "pieces_too_narrow": 0}
    for name, net in old.items():
        code = code_of.get(name)
        if code is None or code not in terminals:
            continue
        stats["nets_matched"] += 1
        cls = meta["nets"][str(code)]["class"]
        old_cls = old_class_of.get(name) if same_grid else None
        rules = meta["classes"][cls]
        if old_cls is not None and old_meta["classes"][old_cls] != rules:
            stats["nets_rules_changed"] += 1  # a width or clearance changed: route the net afresh
            continue

        def open_(cells: np.ndarray) -> bool:
            now = maps.owner(cls, cells)
            closed = (now != FREE) & (now != code)
            if old_cls is not None:
                closed &= now != table[old_maps.owner(old_cls, cells).astype(np.int32) + 2]  # only what the edit changed
            return not bool(np.any(closed))

        def drillable(in_plane: int) -> bool:
            now = int(maps.via[cls][in_plane])
            if now in (FREE, code):
                return True
            return old_cls is not None and now == table[int(old_maps.via[old_cls][in_plane]) + 2]
        pieces: list[tuple[str, int, tuple, np.ndarray]] = []  # kind, index, payload, cells
        for i, (layer_name, width, points) in enumerate(net.wires):
            layer = layer_of.get(layer_name)
            if layer is None:
                continue
            for (xa, ya), (xb, yb) in zip(points, points[1:]):
                steps = max(1, int(math.hypot(xb - xa, yb - ya) / (pitch / 4)))
                walked = [cell(xa + (xb - xa) * s / steps, ya + (yb - ya) * s / steps) for s in range(steps + 1)]
                ordered = list(dict.fromkeys(c for c in walked if c is not None))  # in order along the wire
                if ordered:
                    pieces.append(("wire", i, (layer_name, width, [(xa, ya), (xb, yb)]), np.array(ordered, dtype=np.int64) + layer * plane))
        for i, (padstack, x, y) in enumerate(net.vias):
            c = cell(x, y)
            if c is None:
                continue
            pieces.append(("via", i, (padstack, x, y), np.array([c + l * plane for l in range(layers)], dtype=np.int64)))
        stats["pieces"] += len(pieces)
        usable = []
        for piece in pieces:
            kind, _, payload, cells = piece
            if kind == "wire" and payload[1] < rules["width"] - 1e-6:  # narrower than the net may now be
                stats["pieces_too_narrow"] += 1
                continue
            ok = open_(cells) and (kind != "via" or drillable(int(cells[0] % plane)))
            if ok:
                usable.append(piece)
            else:
                stats["pieces_blocked"] += 1
        # Group usable pieces and terminals: things sharing a cell are joined.
        parent = list(range(len(usable) + len(terminals[code])))

        def find(a: int) -> int:
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        owner_of_cell: dict[int, int] = {}
        for i, (_, _, _, cells) in enumerate(usable):
            for c in cells.tolist():
                j = owner_of_cell.setdefault(c, i)
                if j != i:
                    parent[find(i)] = find(j)
        # Ends that land on a neighbouring cell (rounding at a junction) still join.
        for i, (kind, _, _, cells) in enumerate(usable):
            for c in (cells.tolist() if kind == "via" else [int(cells[0]), int(cells[-1])]):
                for d in (-cols - 1, -cols, -cols + 1, -1, 1, cols - 1, cols, cols + 1):
                    j = owner_of_cell.get(c + d)
                    if j is not None and j != i:
                        parent[find(i)] = find(j)
        base = len(usable)
        for t, cells in enumerate(terminals[code]):
            for c in cells.tolist():
                j = owner_of_cell.get(c)
                if j is not None:
                    parent[find(base + t)] = find(j)
        reach: dict[int, int] = {}
        for t in range(len(terminals[code])):
            root = find(base + t)
            if any(find(i) == root for i in range(base)):
                reach[root] = reach.get(root, 0) + 1
        if not reach:
            stats["nets_not_kept"] += 1
            continue
        best = max(reach, key=reach.get)
        group = _prune([piece for i, piece in enumerate(usable) if find(i) == best], terminals[code], cols)
        stats["pieces_kept"] += len(group)
        if reach[best] == len(terminals[code]) and len(group) == len(pieces):
            stats["nets_fully_kept"] += 1
        else:
            stats["nets_partly_kept"] += 1
        wire_cells = [cells for kind, _, _, cells in group if kind == "wire"]
        via_cells = np.array(sorted({int(cells[0] % plane) for kind, _, _, cells in group if kind == "via"}), dtype=np.int64)
        preset[code] = (np.unique(np.concatenate(wire_cells)) if wire_cells else np.zeros(0, np.int64), via_cells)
        reused = OldNet()
        for kind, _, payload, _ in group:
            if kind == "wire":
                reused.wires.append(payload)
            else:
                reused.vias.append(payload)
        kept[code] = reused
    return Plan(preset, kept, stats)


def _prune(group: list, terminals: list[np.ndarray], cols: int) -> list:
    """Drop dead ends: a wire with an end touching no other piece and no terminal, or a via touched
    by at most one piece, again and again until none is left. Such stubs led to a pad that moved."""
    near = (0, -cols - 1, -cols, -cols + 1, -1, 1, cols - 1, cols, cols + 1)
    terminal_cells = {c for cells in terminals for c in cells.tolist()}
    while True:
        cover: dict[int, set[int]] = {}
        for i, (_, _, _, cells) in enumerate(group):
            for c in cells.tolist():
                cover.setdefault(c, set()).add(i)

        def touched(i: int, c: int) -> bool:
            return any(c + d in terminal_cells or (cover.get(c + d, set()) - {i}) for d in near)

        dead = set()
        for i, (kind, _, _, cells) in enumerate(group):
            if kind == "wire":
                if not touched(i, int(cells[0])) or not touched(i, int(cells[-1])):
                    dead.add(i)
            else:
                others = {j for c in cells.tolist() for d in near for j in cover.get(c + d, set())} - {i}
                if len(others) <= 1 and not any(c + d in terminal_cells for c in cells.tolist() for d in near):
                    dead.add(i)
        if not dead:
            return group
        group = [piece for i, piece in enumerate(group) if i not in dead]


def write_preset(preset: dict[int, tuple[np.ndarray, np.ndarray]], path: str | Path) -> None:
    with open(path, "wb") as sink:
        sink.write(np.array([len(preset)], dtype=np.int32).tobytes())
        for code, (cells, vias) in sorted(preset.items()):
            sink.write(np.array([code, cells.size, vias.size], dtype=np.int32).tobytes())
            sink.write(cells.astype(np.uint32).tobytes())
            sink.write(vias.astype(np.uint32).tobytes())


if __name__ == "__main__":
    import sys

    session, board, meta_path, out = sys.argv[1:5]
    meta = json.loads(Path(meta_path).read_text())
    old_board, old_meta = (sys.argv[5], json.loads(Path(sys.argv[6]).read_text())) if len(sys.argv) > 6 else (None, None)
    result = plan(read_session(session), board, meta, old_board, old_meta)
    write_preset(result.preset, out)
    print(json.dumps(result.stats))
