"""Export a KiCad 10 board for the router (the drop-in's version of tools/kicad_io_fast.py).

Differences from the research exporter: KiCad 10's net class API; copper already on the board
(hand-laid tracks and vias) is kept as fixed copper of its net and offered to the router as
extra terminals; rule-area keep-outs (board and footprint) close cells to tracks and vias.

  python board.py export <board.kicad_pcb> <out.board> <out.meta.json> <options.json> [<previous.board>]

options.json: {"pitch": 0.05, "margin_factor": 0.2, "narrow": [net codes], "min_width": mm}

With a previous export of an earlier version of the same board, only the grid around the items
that changed is drawn again (a patch); the result is the same, byte for byte, as a full export.
Every export leaves `<out.board>.state.npz` and `.state.json` for the next one to patch from.

Router board file (little-endian): int32 layers, rows, cols, class count, net
count, via cost in cells; int32 via blob radius per class and int32 exclusion
radius per ordered class pair, both in thousandths of a cell. Then per class an
int16 trace map [L*R*C], per class an int16 via map [R*C], per class an int16
pad-core map [L*R*C], then the int16 pad map [L*R*C], then per class an int16
via-site map [R*C] (which net alone may drill there, the whole via considered). Then per net: int32 net code, class, pad count; per pad an int32 cell
count and that many uint32 cell indices.

Map values: -1 any net may use the cell, a net code if only that net may, -2 nobody.
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
from matplotlib.path import Path as Outline

PITCH = 0.1  # mm per cell; `export` may override it
MARGIN = 0.2 * PITCH  # mm added to every clearance, to cover cell-centre rounding
VIA_COST_MM = 2.0  # a via costs as much as this much track
MAX_CELLS = 12_000_000
MAX_CLASSES = 6
NARROW_NETS: frozenset[int] = frozenset()  # nets allowed to use the board's minimum track width
NARROW_FLOOR_MM = 0.1
MIN_WIDTH_MM = 0.0  # no track narrower than this (pcbkit's --min-width); classes below it are widened
PATCH_LIMIT = 0.4  # above this share of the board changed, a full export is as quick
FREE, NOBODY = -1, -2
_ABSENT = 32767  # above every net code: stands for "no net" when taking a minimum
ROUTING_FAULTS = ("clearance", "shorting_items", "tracks_crossing", "copper_edge_clearance", "hole_clearance")

type Box = tuple[int, int, int, int]  # rows r0:r1, columns c0:c1


def _mm(value: int) -> float:
    return value / 1e6


def _points(chain) -> np.ndarray:
    return np.array([[_mm(chain.CPoint(i).x), _mm(chain.CPoint(i).y)] for i in range(chain.PointCount())])


@dataclasses.dataclass(frozen=True)
class _Grid:
    x0: float
    y0: float
    rows: int
    cols: int

    def bounds(self, lo: np.ndarray, hi: np.ndarray, reach: float) -> Box | None:
        """The cells a polygon spanning lo-hi can reach, with one cell to spare each side."""
        c0 = max(0, int((lo[0] - reach - self.x0) / PITCH) - 1)
        c1 = min(self.cols, int((hi[0] + reach - self.x0) / PITCH) + 2)
        r0 = max(0, int((lo[1] - reach - self.y0) / PITCH) - 1)
        r1 = min(self.rows, int((hi[1] + reach - self.y0) / PITCH) + 2)
        return (r0, r1, c0, c1) if c0 < c1 and r0 < r1 else None

    def centres(self, box: Box):
        r0, r1, c0, c1 = box
        xs = self.x0 + (np.arange(c0, c1) + 0.5) * PITCH
        ys = self.y0 + (np.arange(r0, r1) + 0.5) * PITCH
        gx, gy = np.meshgrid(xs, ys)
        return (slice(r0, r1), slice(c0, c1)), np.column_stack([gx.ravel(), gy.ravel()]), gx.shape


def _grid_of(x0: float, y0: float, x1: float, y1: float) -> _Grid:
    return _Grid(x0, y0, int(math.ceil((y1 - y0) / PITCH)), int(math.ceil((x1 - x0) / PITCH)))


def _overlap(a: Box, b: Box) -> Box | None:
    r0, r1, c0, c1 = max(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), min(a[3], b[3])
    return (r0, r1, c0, c1) if r0 < r1 and c0 < c1 else None


def _span(boxes) -> Box:
    boxes = list(boxes)
    return (min(b[0] for b in boxes), max(b[1] for b in boxes), min(b[2] for b in boxes), max(b[3] for b in boxes))


def _distance_to_edges(points: np.ndarray, polygon: np.ndarray) -> np.ndarray:
    """Distance from each point to the polygon's boundary."""
    a = polygon
    b = np.roll(polygon, -1, axis=0)
    ab = b - a
    length_sq = np.maximum((ab**2).sum(axis=1), 1e-18)
    ap = points[:, None, :] - a[None, :, :]
    t = np.clip((ap * ab[None]).sum(axis=2) / length_sq[None], 0.0, 1.0)
    nearest = a[None] + t[..., None] * ab[None]
    return np.sqrt(((points[:, None, :] - nearest) ** 2).sum(axis=2)).min(axis=1)


def _field(grid: _Grid, polygon: np.ndarray, reach: float, box: Box | None = None):
    """Inside-ness and distance to the boundary for every cell centre in the polygon's window at `reach`.

    Computed once per polygon for the largest reach needed; `_cut` then gives what a smaller reach
    would give. A cell outside the smaller window is farther than that reach, and each cell's
    distance does not depend on the window, so the result is the same, cell for cell. For the same
    reason `box` may clip the window: the cells left get the values they would have had.
    """
    found = grid.bounds(polygon.min(axis=0), polygon.max(axis=0), reach)
    if found is not None and box is not None:
        found = _overlap(found, box)
    if found is None:
        return None
    where, points, shape = grid.centres(found)
    inside = Outline(polygon).contains_points(points).reshape(shape)
    return where, inside, _distance_to_edges(points, polygon).reshape(shape)


def _cut(field, reach: float):
    if field is None:
        return None
    where, inside, distance = field
    return where, (inside if reach <= 0 else inside | (distance <= reach))


def _edge_lines(board) -> list[np.ndarray]:
    """Every line drawn on the board-edge layer, as open runs of points.

    KiCad's DRC measures copper against each of these, including strays that are
    not part of the closed outline, so the router has to keep away from all of them.
    """
    import pcbnew

    shapes = [item for item in board.GetDrawings() if item.GetLayer() == pcbnew.Edge_Cuts]
    for footprint in board.GetFootprints():
        shapes += [item for item in footprint.GraphicalItems() if item.GetLayer() == pcbnew.Edge_Cuts]
    lines: list[np.ndarray] = []
    for item in shapes:
        if not isinstance(item, pcbnew.PCB_SHAPE):  # targets, text and dimensions are not outline
            continue
        kind = item.GetShape()
        at = lambda point: (_mm(point.x), _mm(point.y))  # noqa: E731
        if kind == pcbnew.SHAPE_T_SEGMENT:
            lines.append(np.array([at(item.GetStart()), at(item.GetEnd())]))
        elif kind == pcbnew.SHAPE_T_RECT:
            (x1, y1), (x2, y2) = at(item.GetStart()), at(item.GetEnd())
            lines.append(np.array([(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]))
        elif kind == pcbnew.SHAPE_T_CIRCLE:
            ring = _circle(at(item.GetCenter()), _mm(item.GetRadius()))
            lines.append(np.vstack([ring, ring[:1]]))
        elif kind == pcbnew.SHAPE_T_ARC:
            centre = np.array(at(item.GetCenter()))
            start, mid, end = (np.array(at(p)) for p in (item.GetStart(), item.GetArcMid(), item.GetEnd()))
            a0, am, a1 = (math.atan2(*(p - centre)[::-1]) for p in (start, mid, end))
            turn = 2 * math.pi
            am = a0 + (am - a0) % turn  # go round the positive way first
            a1 = a0 + (a1 - a0) % turn
            if am > a1:  # the midpoint is not on that side, so the arc runs the other way
                a1 -= turn
            angles = np.linspace(a0, a1, 17)
            radius = float(np.hypot(*(start - centre)))
            lines.append(np.column_stack([centre[0] + radius * np.cos(angles), centre[1] + radius * np.sin(angles)]))
        elif kind == pcbnew.SHAPE_T_POLY:
            poly = item.GetPolyShape()
            for i in range(poly.OutlineCount()):
                ring = _points(poly.Outline(i))
                lines.append(np.vstack([ring, ring[:1]]))
    return lines


def _near_boundary(grid: _Grid, polygon: np.ndarray, reach: float, target: np.ndarray, closed: bool = True) -> None:
    """Mark cells whose centre lies within `reach` of the polygon's boundary, either side.

    Done one edge at a time, each in its own small window: an outline spans the
    whole board, and measuring every cell against every edge at once does not fit
    in memory.
    """
    pairs = zip(polygon, np.roll(polygon, -1, axis=0)) if closed else zip(polygon[:-1], polygon[1:])
    for start, end in pairs:
        found = grid.bounds(np.minimum(start, end), np.maximum(start, end), reach)
        if found is None:
            continue
        where, points, shape = grid.centres(found)
        target[where] |= (_distance_to_edges(points, np.array([start, end])) <= reach).reshape(shape)


def _claim(owner: np.ndarray, where, mask: np.ndarray, net: int) -> None:
    view = owner[where]
    if net == NOBODY:
        view[mask] = NOBODY
        return
    contested = mask & (view != FREE) & (view != net)
    view[mask & (view == FREE)] = net
    view[contested] = NOBODY


def _circle(centre: tuple[float, float], radius: float) -> np.ndarray:
    angles = np.linspace(0, 2 * math.pi, 24, endpoint=False)
    return np.column_stack([centre[0] + radius * np.cos(angles), centre[1] + radius * np.sin(angles)])


def _net_classes(settings) -> dict:
    """Net classes by name, KiCad 10 API; "Default" always present."""
    net_settings = settings.m_NetSettings
    classes = {"Default": net_settings.GetDefaultNetclass()}
    classes |= {str(name): cls for name, cls in net_settings.GetNetclasses().items()}
    return classes


def _pad_shapes(pad, layer_ids: list[int]) -> list[tuple[np.ndarray, list[int]]]:
    """The pad's copper outline on each copper layer it is on, layers with the same outline grouped.

    A pad on no copper layer (a plain hole) gets its front-layer outline and no layers, so its hole
    is still kept clear.
    """
    on = [i for i, layer in enumerate(layer_ids) if pad.IsOnLayer(layer)]
    groups: list[tuple[np.ndarray, list[int]]] = []
    for i in on or [0]:
        polygon_set = pad.GetEffectivePolygon(layer_ids[i])
        if not polygon_set.OutlineCount():
            continue
        polygon = _points(polygon_set.Outline(0))
        for known, layers in groups:
            if known.shape == polygon.shape and np.allclose(known, polygon):
                if on:
                    layers.append(i)
                break
        else:
            groups.append((polygon, [i] if on else []))
    return groups


def _capsule(start: tuple[float, float], end: tuple[float, float], radius: float) -> np.ndarray:
    """Outline of a track: the points within `radius` of the segment start-end."""
    (x1, y1), (x2, y2) = start, end
    heading = math.atan2(y2 - y1, x2 - x1)
    left = np.linspace(heading + math.pi / 2, heading + 3 * math.pi / 2, 9)
    right = np.linspace(heading - math.pi / 2, heading + math.pi / 2, 9)
    return np.vstack([
        np.column_stack([x1 + radius * np.cos(left), y1 + radius * np.sin(left)]),
        np.column_stack([x2 + radius * np.cos(right), y2 + radius * np.sin(right)]),
    ])


# What the board holds, as plain records. Each item's writes to the grid depend only on its own
# fields and the board-level context, so an item whose signature is unchanged writes what it wrote.


@dataclasses.dataclass(frozen=True)
class _Pad:
    code: int  # NOBODY when the pad has no net
    name: str  # net name; signatures use it, as codes are renumbered between saves
    groups: tuple  # ((outline, on-layer indices), ...)
    on_all: tuple
    own_clearance: float
    drill: float
    centre: tuple
    npth: bool
    routed: bool  # its net has two pads or more, so the pad is a terminal


@dataclasses.dataclass(frozen=True)
class _Fixed:  # a hand-laid track or via
    code: int
    name: str
    polygon: np.ndarray
    on: tuple
    own_clearance: float
    hole: tuple | None  # (centre, radius) of a via's drill


@dataclasses.dataclass(frozen=True)
class _Keepout:
    polygons: tuple
    on: tuple
    tracks: bool
    vias: bool


@dataclasses.dataclass(frozen=True)
class _Drawing:
    rectangle: np.ndarray
    layer: int


type _Item = _Pad | _Fixed | _Keepout | _Drawing


@dataclasses.dataclass(frozen=True)
class _Context:
    grid: _Grid
    shape: tuple[int, int, int]
    classes: tuple
    hole_to_hole: float

    def largest(self, own_clearance: float) -> float:
        return max(max(w, v) / 2 + max(cl, own_clearance) + MARGIN for w, cl, v, _ in self.classes)

    def no_drill(self, centre, radius: float) -> tuple[np.ndarray, float]:
        """No new via, of any net, closer to an existing hole than the board's hole-to-hole minimum."""
        return _circle(centre, max(radius, PITCH / 2)), max(d for *_, d in self.classes) / 2 + self.hole_to_hole + MARGIN


@dataclasses.dataclass(frozen=True)
class _Maps:
    trace: list
    via: list
    core: list
    pad_net: np.ndarray
    in_keepout: np.ndarray  # cells inside a track keep-out (a pour's place)
    via_site: list  # per class: the net alone that may drill here, its whole via considered


@dataclasses.dataclass(frozen=True)
class _Terminal:
    cells: np.ndarray  # sorted flat cell indices
    anchor: dict


def _signature(item: _Item) -> str:
    digest = hashlib.blake2b(digest_size=12)

    def feed(value) -> None:
        if isinstance(value, np.ndarray):
            digest.update(b"A" + np.ascontiguousarray(value, dtype=np.float64).tobytes())
        elif isinstance(value, (tuple, list)):
            digest.update(b"(")
            for part in value:
                feed(part)
            digest.update(b")")
        else:
            digest.update(repr(value).encode() + b";")

    feed(type(item).__name__)
    for field in dataclasses.fields(item):
        if field.name != "code":
            feed(getattr(item, field.name))
    return digest.hexdigest()


def _stamps(item: _Item, ctx: _Context) -> list[tuple[np.ndarray, float]]:
    """The outlines an item draws, each with the farthest reach it is drawn at."""
    match item:
        case _Pad():
            largest = ctx.largest(item.own_clearance)
            stamps = [(polygon, largest) for polygon, _ in item.groups]
            if item.drill > 0:
                stamps += [(_circle(item.centre, item.drill / 2), largest), ctx.no_drill(item.centre, item.drill / 2)]
            return stamps
        case _Fixed():
            stamps = [(item.polygon, ctx.largest(item.own_clearance))]
            return stamps + ([ctx.no_drill(*item.hole)] if item.hole else [])
        case _Keepout():
            widest = max(max(w, v) / 2 for w, _, v, _ in ctx.classes) + MARGIN
            return [(polygon, widest) for polygon in item.polygons]
        case _Drawing():
            return [(item.rectangle, max(max(w, v) / 2 + cl + MARGIN for w, cl, v, _ in ctx.classes))]


def _box(item: _Item, ctx: _Context) -> Box | None:
    """Every cell the item can write to lies in this box."""
    found = [b for polygon, reach in _stamps(item, ctx) if (b := ctx.grid.bounds(polygon.min(axis=0), polygon.max(axis=0), reach))]
    return _span(found) if found else None


def _flat(ctx: _Context, layer: int, where, mask: np.ndarray) -> np.ndarray:
    rows, cols = np.nonzero(mask)
    plane = ctx.shape[1] * ctx.shape[2]
    return layer * plane + (rows + where[0].start) * ctx.shape[2] + cols + where[1].start


def _apply(item: _Item, ctx: _Context, maps: _Maps, box: Box | None) -> list[np.ndarray]:
    """Draw the item onto the maps, within `box` if given; returns the cells of its own copper."""
    fields = [_field(ctx.grid, polygon, reach, box) for polygon, reach in _stamps(item, ctx)]
    classes, layers = ctx.classes, ctx.shape[0]
    trace, via, core = maps.trace, maps.via, maps.core
    copper: list[np.ndarray] = []

    def no_drill(field) -> None:
        for c, (*_, via_drill) in enumerate(classes):
            found = _cut(field, via_drill / 2 + ctx.hole_to_hole + MARGIN)
            if found is not None:
                _claim(via[c], *found, NOBODY)

    def own_copper(field, on, code) -> None:
        found = _cut(field, 0.0)
        if found is None:
            return
        where, mask = found
        for layer in on:
            maps.pad_net[layer][where][mask] = code
            copper.append(_flat(ctx, layer, where, mask))
            if code != NOBODY:
                for c, (width, *_rest) in enumerate(classes):
                    core[c][layer][where][mask & (field[2] >= width / 2)] = code

    match item:
        case _Pad():
            code, groups = item.code, item.groups
            for (_, on), field in zip(groups, fields):  # a KiCad 10 pad may have a different shape on each layer
                for c, (width, clearance, via_diameter, _) in enumerate(classes):
                    gap = max(clearance, item.own_clearance) + MARGIN
                    found = _cut(field, width / 2 + gap)
                    if found is not None:
                        for layer in on:
                            _claim(trace[c][layer], *found, code)
                    found = _cut(field, via_diameter / 2 + gap)
                    if found is not None and on:
                        _claim(via[c], *found, code)
                own_copper(field, on, code)
            if item.drill > 0:  # a drilled hole keeps copper away on every layer
                hole_field, hole_owner = fields[len(groups)], NOBODY if item.npth else code
                for c, (width, clearance, via_diameter, _) in enumerate(classes):
                    gap = max(clearance, item.own_clearance) + MARGIN
                    found = _cut(hole_field, width / 2 + gap)
                    if found is not None:
                        for layer in range(layers):
                            _claim(trace[c][layer], *found, hole_owner)
                    found = _cut(hole_field, via_diameter / 2 + gap)
                    if found is not None:
                        _claim(via[c], *found, hole_owner)
                no_drill(fields[len(groups) + 1])
            elif item.on_all:  # no via in or touching a surface-mount pad, even of its own net
                for field in fields[: len(groups)]:
                    for c, (_, _, via_diameter, _) in enumerate(classes):
                        found = _cut(field, via_diameter / 2 + MARGIN)
                        if found is not None:
                            _claim(via[c], *found, NOBODY)
        case _Fixed():
            if item.hole:
                no_drill(fields[1])
            field = fields[0]
            for c, (width, clearance, via_diameter, _) in enumerate(classes):
                gap = max(clearance, item.own_clearance) + MARGIN
                found = _cut(field, width / 2 + gap)
                if found is not None:
                    for layer in item.on:
                        _claim(trace[c][layer], *found, item.code)
                found = _cut(field, via_diameter / 2 + gap)
                if found is not None:
                    _claim(via[c], *found, item.code)
            if item.code != NOBODY:
                own_copper(field, item.on, item.code)
        case _Keepout():
            for field in fields:
                inside = _cut(field, 0.0)
                if inside is not None and item.tracks:
                    for layer in item.on:
                        maps.in_keepout[layer][inside[0]] |= inside[1]
                for c, (width, _, via_diameter, _) in enumerate(classes):
                    if item.tracks:
                        found = _cut(field, width / 2 + MARGIN)
                        if found is not None:
                            for layer in item.on:
                                _claim(trace[c][layer], *found, NOBODY)
                                core[c][layer][found[0]][found[1]] = FREE
                    if item.vias:
                        found = _cut(field, via_diameter / 2 + MARGIN)
                        if found is not None:
                            _claim(via[c], *found, NOBODY)
        case _Drawing():
            field = fields[0]
            for c, (width, clearance, via_diameter, _) in enumerate(classes):
                found = _cut(field, width / 2 + clearance + MARGIN)
                if found is not None:
                    _claim(trace[c][item.layer], *found, NOBODY)
                found = _cut(field, via_diameter / 2 + clearance + MARGIN)
                if found is not None:
                    _claim(via[c], *found, NOBODY)
                found = _cut(field, 0.0)
                if found is not None:
                    core[c][item.layer][found[0]][found[1]] = FREE
    return copper


def _terminal(item: _Item, ctx: _Context, maps: _Maps, copper: list[np.ndarray] | None, known: _Terminal | None,
              box: Box | None) -> _Terminal | None:
    """The item as a terminal of its net, from the cells just drawn, or `known` when they were clipped.

    A pad that covers no cell centre gets the cell under its middle, written to the pad map after
    the pad's own writes, as a full export does.
    """
    match item:
        case _Pad(routed=True):
            if known is None:
                cells = np.unique(np.concatenate(copper)) if copper else np.zeros(0, np.int64)
                middle = item.groups[0][0].mean(axis=0)
                known = _Terminal(cells.astype(np.uint32), {"x": float(middle[0]), "y": float(middle[1]), "exact": bool(cells.size)})
                if not cells.size:
                    row, col = _anchor_cell(ctx, known.anchor)
                    plane = ctx.shape[1] * ctx.shape[2]
                    known = dataclasses.replace(known, cells=np.array([layer * plane + row * ctx.shape[2] + col for layer in item.on_all], np.uint32))
            if not known.anchor["exact"]:
                row, col = _anchor_cell(ctx, known.anchor)
                if box is None or (box[0] <= row < box[1] and box[2] <= col < box[3]):
                    for layer in item.on_all:
                        maps.pad_net[layer, row, col] = item.code
            return known
        case _Fixed() if item.code != NOBODY:
            if known is None:
                cells = np.unique(np.concatenate(copper)) if copper else np.zeros(0, np.int64)
                middle = item.polygon.mean(axis=0)
                known = _Terminal(cells.astype(np.uint32), {"x": float(middle[0]), "y": float(middle[1]), "exact": True, "fixed": True})
            return known
    return None


def _has_terminal(item: _Item) -> bool:
    return (isinstance(item, _Pad) and item.routed) or (isinstance(item, _Fixed) and item.code != NOBODY)


def _anchor_cell(ctx: _Context, anchor: dict) -> tuple[int, int]:
    row = min(ctx.grid.rows - 1, max(0, int((anchor["y"] - ctx.grid.y0) / PITCH)))
    col = min(ctx.grid.cols - 1, max(0, int((anchor["x"] - ctx.grid.x0) / PITCH)))
    return row, col


def _maybe_touching(terms: list[np.ndarray], cols: int, plane: int) -> np.ndarray:
    """The terminals that may touch another: on a common layer, with boxes a cell apart or less.

    A cheap filter before the exact test. Flat cell indices wrap from one row to the next and one
    layer to the next; a terminal on the grid's border could touch across a wrap, so then every
    terminal is tested.
    """
    count = len(terms)
    layers = max(1, int(max((t[-1] for t in terms if t.size), default=0)) // plane + 1)
    box = np.zeros((count, layers, 4), dtype=np.int64)  # row min, row max, column min, column max
    box[..., 0::2], box[..., 1::2] = 1 << 40, -(1 << 40)
    rows = plane // cols
    for i, cells in enumerate(terms):
        if not cells.size:
            continue
        cells = cells.astype(np.int64)
        layer, row, col = cells // plane, (cells % plane) // cols, cells % cols
        if row.min() == 0 or row.max() == rows - 1 or col.min() == 0 or col.max() == cols - 1:
            return np.arange(count)
        for k in np.unique(layer).tolist():
            here = layer == k
            box[i, k] = (row[here].min(), row[here].max(), col[here].min(), col[here].max())
    a, b = box[:, None], box[None, :]
    near = ((a[..., 0] <= b[..., 1] + 1) & (b[..., 0] <= a[..., 1] + 1) & (a[..., 2] <= b[..., 3] + 1) & (b[..., 2] <= a[..., 3] + 1)).any(axis=2)
    np.fill_diagonal(near, False)
    return np.flatnonzero(near.any(axis=1))


def _merge(terms: list[np.ndarray], marks: list[dict], cols: int, plane: int) -> tuple[list[np.ndarray], list[dict]]:
    """Terminals of a net that touch (a pad and the hand-laid via on it, a stub and its pad) are one
    terminal: copper already joins them. Touching means sharing a cell or being 8-neighbours."""
    if len(terms) < 2:
        return terms, marks
    involved = _maybe_touching(terms, cols, plane)
    if not involved.size:
        return terms, marks
    labels = np.concatenate([np.full(terms[i].size, i, dtype=np.int64) for i in involved])
    cells = np.concatenate([terms[i] for i in involved]).astype(np.int64)
    order = np.argsort(cells, kind="stable")
    sorted_cells, sorted_labels = cells[order], labels[order]
    offsets = np.array([0, -cols - 1, -cols, -cols + 1, -1, 1, cols - 1, cols, cols + 1], dtype=np.int64)
    asked = (cells[:, None] + offsets[None]).ravel()
    asker = np.repeat(labels, len(offsets))
    at = np.minimum(np.searchsorted(sorted_cells, asked), len(sorted_cells) - 1)
    hit = (sorted_cells[at] == asked) & (sorted_labels[at] != asker)
    parent = list(range(len(terms)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b in np.unique(np.column_stack([asker[hit], sorted_labels[at][hit]]), axis=0).tolist():
        parent[find(a)] = find(b)
    groups: dict[int, list[int]] = {}
    for i in range(len(terms)):
        groups.setdefault(find(i), []).append(i)
    if len(groups) == len(terms):
        return terms, marks
    merged_cells, merged_marks = [], []
    for members in groups.values():
        merged_cells.append(np.unique(np.concatenate([terms[i] for i in members])))
        pads_first = sorted(members, key=lambda i: marks[i].get("fixed", False))
        merged_marks.append(dict(marks[pads_first[0]], exact=any(marks[i]["exact"] for i in members)))
    return merged_cells, merged_marks


def _read(board_path: str):
    """The board's context, items and naming, or the reason it cannot be routed."""
    import pcbnew

    board = pcbnew.LoadBoard(board_path)
    settings = board.GetDesignSettings()
    layer_ids = [int(layer) for layer in board.GetEnabledLayers().CuStack()]
    class_by_name = _net_classes(settings)

    def rules(name: str) -> tuple[float, float, float, float]:
        cls = class_by_name.get(name, class_by_name["Default"])
        return (_mm(cls.GetTrackWidth()), _mm(cls.GetClearance()), _mm(cls.GetViaDiameter()), _mm(cls.GetViaDrill()))

    pads = list(board.GetPads())
    pads_of: dict[int, list] = collections.defaultdict(list)
    for pad in pads:
        if pad.GetNetCode() > 0 and any(pad.IsOnLayer(layer) for layer in layer_ids):
            pads_of[pad.GetNetCode()].append(pad)
    routed = {code: group for code, group in pads_of.items() if len(group) >= 2}
    net_rules = {code: rules(group[0].GetNetClassName()) for code, group in routed.items()}
    # A net that cannot be routed at its class's default width may use the board's own minimum width.
    board_min_width = max(NARROW_FLOOR_MM, _mm(settings.m_TrackMinWidth))
    for code in NARROW_NETS & set(net_rules):
        width, *rest = net_rules[code]
        net_rules[code] = (min(width, board_min_width), *rest)
    if MIN_WIDTH_MM > 0:
        net_rules = {code: (max(width, MIN_WIDTH_MM), *rest) for code, (width, *rest) in net_rules.items()}
    classes = tuple(sorted(set(net_rules.values())))
    if not routed or len(classes) > MAX_CLASSES or max(pads_of, default=0) > 32000:
        return {"skipped": "no nets" if not routed else f"{len(classes)} net classes"}
    names = {code: str(net.GetNetname()) for code, net in board.GetNetsByNetcode().items() if code > 0}

    outline = pcbnew.SHAPE_POLY_SET()
    has_outline = bool(board.GetBoardPolygonOutlines(outline, False)) and outline.OutlineCount() > 0
    shapes = [(pad, groups) for pad in pads if (groups := _pad_shapes(pad, layer_ids))]
    edge_lines = _edge_lines(board)
    every = np.vstack([polygon for _, groups in shapes for polygon, _ in groups])
    lo, hi = every.min(axis=0), every.max(axis=0)
    if edge_lines:  # take in edge lines close to the pads, but not a drawing frame far away
        drawn = np.vstack(edge_lines)
        close = drawn[((drawn >= lo - 10.0) & (drawn <= hi + 10.0)).all(axis=1)]
        if len(close):
            lo, hi = np.minimum(lo, close.min(axis=0)), np.maximum(hi, close.max(axis=0))
    rings: list[tuple[np.ndarray, bool]] = []
    if has_outline:  # a closed outline also tells inside from outside
        for i in range(outline.OutlineCount()):
            rings.append((_points(outline.Outline(i)), False))
            rings += [(_points(outline.Hole(i, j)), True) for j in range(outline.HoleCount(i))]
        outer = np.vstack([ring for ring, is_hole in rings if not is_hole])
        lo, hi = np.minimum(lo, outer.min(axis=0)), np.maximum(hi, outer.max(axis=0))
    grid = _grid_of(lo[0] - 1.0, lo[1] - 1.0, hi[0] + 1.0, hi[1] + 1.0)
    shape = (len(layer_ids), grid.rows, grid.cols)
    if np.prod(shape) > MAX_CELLS:
        return {"skipped": "board too large for this test"}
    ctx = _Context(grid, shape, classes, _mm(settings.m_HoleToHoleMin))
    edge_clearance = _mm(class_by_name["Default"].GetClearance())

    items: list[_Item] = []
    for pad, groups in shapes:
        code = pad.GetNetCode() if pad.GetNetCode() > 0 else NOBODY
        on_all = tuple(i for i, layer in enumerate(layer_ids) if pad.IsOnLayer(layer))
        items.append(_Pad(
            code=code,
            name=names.get(code, ""),
            groups=tuple((polygon, tuple(on)) for polygon, on in groups),
            on_all=on_all,
            own_clearance=max((_mm(pad.GetOwnClearance(layer_ids[i])) for i in on_all), default=0.0),
            drill=max(_mm(pad.GetDrillSize().x), _mm(pad.GetDrillSize().y)),
            centre=(_mm(pad.GetPosition().x), _mm(pad.GetPosition().y)),
            npth=pad.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH,
            routed=code in routed,
        ))
    # Copper already on the board (hand-laid tracks and vias) stays where it is. It keeps other nets
    # away as a pad does, and joins its own net as an extra terminal the router connects to.
    for track in board.GetTracks():
        code = track.GetNetCode() if track.GetNetCode() > 0 else NOBODY
        own_clearance = _mm(class_by_name.get(str(track.GetNetClassName()), class_by_name["Default"]).GetClearance())
        if track.Type() == pcbnew.PCB_VIA_T:
            centre = (_mm(track.GetPosition().x), _mm(track.GetPosition().y))
            polygon = _circle(centre, _mm(track.GetWidth(layer_ids[0])) / 2)
            on, hole = tuple(range(shape[0])), (centre, _mm(track.GetDrillValue()) / 2)
        else:
            if int(track.GetLayer()) not in layer_ids:
                continue
            start = (_mm(track.GetStart().x), _mm(track.GetStart().y))
            end = (_mm(track.GetEnd().x), _mm(track.GetEnd().y))
            radius = _mm(track.GetWidth()) / 2
            if track.Type() == pcbnew.PCB_ARC_T:  # an arc as its chord, widened by the bulge
                mid = (_mm(track.GetMid().x), _mm(track.GetMid().y))
                chord_mid = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
                radius += math.hypot(mid[0] - chord_mid[0], mid[1] - chord_mid[1])
            polygon = _capsule(start, end, radius)
            on, hole = (layer_ids.index(int(track.GetLayer())),), None
        items.append(_Fixed(code, names.get(code, ""), polygon, on, own_clearance, hole))
    # Rule-area keep-outs, on the board or inside footprints, close their area to tracks and/or vias.
    areas = list(board.Zones())
    for footprint in board.GetFootprints():
        areas += list(footprint.Zones())
    for zone in areas:
        if not zone.GetIsRuleArea() or not (zone.GetDoNotAllowTracks() or zone.GetDoNotAllowVias()):
            continue
        on = tuple(layer_ids.index(int(layer)) for layer in zone.GetLayerSet().CuStack() if int(layer) in layer_ids)
        if on:
            area = zone.Outline()
            polygons = tuple(_points(area.Outline(i)) for i in range(area.OutlineCount()))
            items.append(_Keepout(polygons, on, bool(zone.GetDoNotAllowTracks()), bool(zone.GetDoNotAllowVias())))
    # Text and drawings on copper layers are copper too. Their bounding boxes are kept clear.
    drawn_items = list(board.GetDrawings())
    for footprint in board.GetFootprints():
        drawn_items += list(footprint.GraphicalItems())
    for drawing in drawn_items:
        if int(drawing.GetLayer()) not in layer_ids:
            continue
        box = drawing.GetBoundingBox()
        left, top, right, bottom = (_mm(v) for v in (box.GetLeft(), box.GetTop(), box.GetRight(), box.GetBottom()))
        if right - left > 200 or bottom - top > 200:
            continue
        rectangle = np.array([(left, top), (right, top), (right, bottom), (left, bottom)])
        items.append(_Drawing(rectangle, layer_ids.index(int(drawing.GetLayer()))))

    level = hashlib.blake2b(digest_size=12)  # what every cell depends on: if it changes, nothing is reused
    level.update(repr((grid, shape, classes, ctx.hole_to_hole, edge_clearance, PITCH, MARGIN, has_outline)).encode())
    for ring, is_hole in rings:
        level.update(repr(is_hole).encode() + ring.tobytes())
    for line in edge_lines:
        level.update(b"L" + line.tobytes())
    return {
        "skipped": None,
        "ctx": ctx,
        "items": items,
        "level": level.hexdigest(),
        "rings": rings,
        "edge_lines": edge_lines,
        "edge_clearance": edge_clearance,
        "has_outline": has_outline,
        "routed": sorted(routed),
        "class_of": {code: classes.index(rule) for code, rule in net_rules.items()},
        "pads_of": {code: len(group) for code, group in routed.items()},
        "names": names,
        "layer_ids": layer_ids,
        "layer_names": [str(board.GetLayerName(layer)) for layer in layer_ids],
        "min_width": board_min_width,
    }


def _bands(read: dict) -> tuple[np.ndarray, np.ndarray]:
    """Per class, the cells a track (first) or a via (second) may not use for being off the board
    or too near its edge."""
    ctx, rings, edge_lines = read["ctx"], read["rings"], read["edge_lines"]
    grid, shape = ctx.grid, ctx.shape
    outside = np.zeros(shape[1:], dtype=bool)
    if read["has_outline"]:
        _, centres, plane_shape = grid.centres((0, grid.rows, 0, grid.cols))
        inside = np.zeros(len(centres), dtype=bool)
        for ring, is_hole in rings:
            if not is_hole:
                inside |= Outline(ring).contains_points(centres)
        for ring, is_hole in rings:
            if is_hole:
                inside &= ~Outline(ring).contains_points(centres)
        outside = ~inside.reshape(plane_shape)
    track_band = np.zeros((len(ctx.classes), *shape[1:]), dtype=bool)
    via_band = np.zeros_like(track_band)
    for c, (width, _, via_diameter, _) in enumerate(ctx.classes):
        for reach, target in ((read["edge_clearance"] + width / 2 + MARGIN, track_band[c]), (read["edge_clearance"] + via_diameter / 2 + MARGIN, via_band[c])):
            target |= outside
            for ring, _ in rings:
                _near_boundary(grid, ring, reach, target)
            for line in edge_lines:
                _near_boundary(grid, line, reach, target, closed=False)
    return track_band, via_band


def _reset(maps: _Maps, track_band: np.ndarray, via_band: np.ndarray, box: Box) -> None:
    """Back to the bare board within `box`: free, except off the board and near its edge."""
    r, c = slice(box[0], box[1]), slice(box[2], box[3])
    for k in range(len(maps.trace)):
        maps.trace[k][:, r, c] = np.where(track_band[k][r, c], NOBODY, FREE)[None]
        maps.via[k][r, c] = np.where(via_band[k][r, c], NOBODY, FREE)
        maps.core[k][:, r, c] = FREE
    maps.pad_net[:, r, c] = FREE
    maps.in_keepout[:, r, c] = False


def _finish(maps: _Maps, track_band: np.ndarray, box: Box) -> None:
    """The edge rule holds even over a net's own pad: KiCad reports the track, not the pad."""
    r, c = slice(box[0], box[1]), slice(box[2], box[3])
    for k, core in enumerate(maps.core):
        for layer in range(core.shape[0]):
            core[layer, r, c][track_band[k][r, c]] = FREE


def _disc_rows(radius: float) -> list[int]:
    """Half-width of the via disc in each row from -reach to reach, as the router builds the disc."""
    reach, limit = int(math.floor(radius)), radius * radius
    widths = []
    for dr in range(-reach, reach + 1):
        w = 0
        while (w + 1) ** 2 + dr * dr <= limit:
            w += 1
        widths.append(w)
    return widths


def _via_site(maps: _Maps, c: int, radius: float, box: Box) -> np.ndarray:
    """Within `box`, which net alone may drill a via of class `c` there.

    A via is a disc of track centres (`radius` cells), on every layer: no one may drill if any cell
    of the disc is closed or off the grid, or if two nets own cells of it (or the via map's own
    cell); else the one net that owns any, or anyone if none does. That is the minimum and maximum
    owner over the disc, with "anyone" left out, so it is taken row by row of the disc.
    """
    r0, r1, c0, c1 = box
    rows, cols = maps.via[c].shape
    widths = _disc_rows(radius)
    reach = len(widths) // 2
    lo = np.full((r1 - r0 + 2 * reach, c1 - c0 + 2 * reach), NOBODY, dtype=np.int16)  # off the grid: no one
    hi = np.full(lo.shape, FREE, dtype=np.int16)
    s0, s1, t0, t1 = max(r0 - reach, 0), min(r1 + reach, rows), max(c0 - reach, 0), min(c1 + reach, cols)
    trace, core = maps.trace[c][:, s0:s1, t0:t1], maps.core[c][:, s0:s1, t0:t1]
    owner = np.where((trace == NOBODY) & (core >= 0), core, trace)
    inner = (slice(s0 - r0 + reach, s1 - r0 + reach), slice(t0 - c0 + reach, t1 - c0 + reach))
    lo[inner] = np.where(owner == FREE, _ABSENT, owner).min(axis=0)
    hi[inner] = owner.max(axis=0)
    height, width = r1 - r0, c1 - c0
    across: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    for w in sorted(set(widths)):  # along each row first
        low, high = lo[:, reach - w : reach - w + width].copy(), hi[:, reach - w : reach - w + width].copy()
        for dc in range(-w + 1, w + 1):
            np.minimum(low, lo[:, reach + dc : reach + dc + width], out=low)
            np.maximum(high, hi[:, reach + dc : reach + dc + width], out=high)
        across[w] = (low, high)
    own = maps.via[c][r0:r1, c0:c1]
    low, high = np.where(own == FREE, _ABSENT, own).astype(np.int16), own.copy()
    for dr, w in zip(range(-reach, reach + 1), widths):  # then down the disc's rows
        np.minimum(low, across[w][0][reach + dr : reach + dr + height], out=low)
        np.maximum(high, across[w][1][reach + dr : reach + dr + height], out=high)
    return np.where(low == NOBODY, NOBODY, np.where(low == _ABSENT, FREE, np.where(low == high, low, NOBODY))).astype(np.int16)


def _merge_boxes(boxes: list[Box]) -> list[Box]:
    """Disjoint boxes covering `boxes`: overlapping ones are replaced by the box spanning both."""
    merged: list[Box] = []
    for box in boxes:
        while True:
            hit = next((i for i, other in enumerate(merged) if _overlap(box, other)), None)
            if hit is None:
                break
            box = _span([box, merged.pop(hit)])
        merged.append(box)
    return merged


def _load_previous(previous: str | None, read: dict):
    """The previous export's maps and state, if it can be patched to this board; else None."""
    if not previous:
        return None
    state_json, state_npz = Path(previous + ".state.json"), Path(previous + ".state.npz")
    if not (state_json.exists() and state_npz.exists() and Path(previous).exists()):
        return None
    state = json.loads(state_json.read_text())
    if state["level"] != read["level"]:
        return None
    arrays = np.load(state_npz)
    ctx = read["ctx"]
    n, plane_shape = len(ctx.classes), ctx.shape[1:]
    unpack = lambda packed, shape: np.unpackbits(packed, count=int(np.prod(shape))).astype(bool).reshape(shape)  # noqa: E731
    raw = np.fromfile(previous, dtype=np.int16, offset=4 * (6 + n + n * n))
    size, plane = int(np.prod(ctx.shape)), int(np.prod(plane_shape))
    offsets = np.cumsum([0] + [size] * n + [plane] * n + [size] * n + [size] + [plane] * n)
    pieces = [raw[offsets[i] : offsets[i + 1]].copy() for i in range(len(offsets) - 1)]
    # Net codes are renumbered between saves: translate the old codes by name.
    code_of = {name: int(code) for code, name in read["names"].items()}
    old_names = {int(code): name for code, name in state["names"].items()}
    table = np.full(max(old_names, default=0) + 3, NOBODY, dtype=np.int16)
    table[0], table[1] = NOBODY, FREE
    for old, name in old_names.items():
        table[old + 2] = code_of.get(name, NOBODY)  # a net now gone: its items changed, so its cells are redrawn
    if any(table[old + 2] != old for old in old_names):
        pieces = [table[piece + 2] for piece in pieces]  # codes stay below 32000, so +2 fits in int16
    maps = _Maps(
        trace=[p.reshape(ctx.shape) for p in pieces[:n]],
        via=[p.reshape(plane_shape) for p in pieces[n : 2 * n]],
        core=[p.reshape(ctx.shape) for p in pieces[2 * n : 3 * n]],
        pad_net=pieces[3 * n].reshape(ctx.shape),
        in_keepout=unpack(arrays["in_keepout"], ctx.shape),
        via_site=[p.reshape(plane_shape) for p in pieces[3 * n + 1 :]],
    )
    bands = (unpack(arrays["track_band"], (n, *plane_shape)), unpack(arrays["via_band"], (n, *plane_shape)))
    cells, ends = arrays["terminal_cells"], arrays["terminal_ends"]
    terminals = {sig: _Terminal(cells[ends[i] - size_ : ends[i]], anchor)
                 for i, (sig, size_, anchor) in enumerate(state["terminals"])}
    return maps, bands, [(sig, tuple(box) if box else None) for sig, box in state["items"]], terminals


def export(board_path: str, out_path: str, meta_path: str, previous: str | None = None) -> None:
    started = time.monotonic()
    read = _read(board_path)
    meta: dict = {"board": board_path, "skipped": read["skipped"]}
    if read["skipped"]:
        Path(meta_path).write_text(json.dumps(meta))
        return
    ctx, items = read["ctx"], read["items"]
    shape, classes = ctx.shape, ctx.classes
    signatures = [_signature(item) for item in items]
    boxes = [_box(item, ctx) for item in items]
    terminals: list[_Terminal | None] = [None] * len(items)
    # A via is modelled as a disc of track centres. One cell is added because the disc is made of
    # cells: with less, a lattice disc can fall short of the true via edge in some directions.
    blobs = [round(1000 * (max(0.0, (via_diameter - width) / 2 / PITCH) + 1.0)) for width, _, via_diameter, _ in classes]
    radii = [round(1000 * (a[0] / 2 + b[0] / 2 + max(a[1], b[1]) + MARGIN) / PITCH) for a in classes for b in classes]
    reused = _load_previous(previous, read)
    mode = "full"
    if reused is not None:
        maps, (track_band, via_band), old_items, known = reused
        before, now = collections.Counter(sig for sig, _ in old_items), collections.Counter(signatures)
        changed = {sig for sig in before | now if before[sig] != now[sig]}
        region = _merge_boxes([box for sig, box in old_items if sig in changed and box]
                              + [box for sig, box in zip(signatures, boxes) if sig in changed and box])
        area = sum((b[1] - b[0]) * (b[3] - b[2]) for b in region)
        reusable = all(sig in known for sig, item in zip(signatures, items) if sig not in changed and _has_terminal(item))
        if area <= PATCH_LIMIT * shape[1] * shape[2] and reusable:
            mode = f"patch ({len(changed)} items changed, {len(region)} areas, {area / (shape[1] * shape[2]):.1%} of the board)"
            for box in region:
                _reset(maps, track_band, via_band, box)
            for i, item in enumerate(items):
                fresh = signatures[i] in changed
                for box in region:
                    if boxes[i] is None or not _overlap(boxes[i], box):
                        continue
                    copper = _apply(item, ctx, maps, box)
                    terminals[i] = _terminal(item, ctx, maps, copper if fresh else None, terminals[i] or (None if fresh else known.get(signatures[i])), box)
                if terminals[i] is None and not fresh:
                    terminals[i] = known.get(signatures[i])
            for box in region:
                _finish(maps, track_band, box)
            for c, blob in enumerate(blobs):
                reach = len(_disc_rows(blob / 1000)) // 2  # a via site sees this far
                for r0, r1, c0, c1 in region:
                    near = (max(0, r0 - reach), min(shape[1], r1 + reach), max(0, c0 - reach), min(shape[2], c1 + reach))
                    maps.via_site[c][near[0] : near[1], near[2] : near[3]] = _via_site(maps, c, blob / 1000, near)
        else:
            reused = None
    if reused is None:
        n = len(classes)
        track_band, via_band = _bands(read)
        maps = _Maps(
            trace=[np.where(track_band[c][None], NOBODY, FREE).astype(np.int16).repeat(shape[0], axis=0) for c in range(n)],
            via=[np.where(via_band[c], NOBODY, FREE).astype(np.int16) for c in range(n)],
            core=[np.full(shape, FREE, dtype=np.int16) for _ in classes],
            pad_net=np.full(shape, FREE, dtype=np.int16),
            in_keepout=np.zeros(shape, dtype=bool),
            via_site=[],
        )
        for i, item in enumerate(items):
            terminals[i] = _terminal(item, ctx, maps, _apply(item, ctx, maps, None), None, None)
        whole = (0, shape[1], 0, shape[2])
        _finish(maps, track_band, whole)
        maps.via_site.extend(_via_site(maps, c, blob / 1000, whole) for c, blob in enumerate(blobs))
    trace, core = maps.trace, maps.core

    routed, class_of = read["routed"], read["class_of"]
    net_pads: dict[int, list[np.ndarray]] = {code: [] for code in routed}
    anchors: dict[int, list[dict]] = {code: [] for code in routed}
    for item, terminal in zip(items, terminals):
        if terminal is not None and item.code in net_pads and (isinstance(item, _Pad) or terminal.cells.size):
            net_pads[item.code].append(terminal.cells)
            anchors[item.code].append(terminal.anchor)
    for code in routed:
        net_pads[code], anchors[code] = _merge(net_pads[code], anchors[code], shape[2], shape[1] * shape[2])

    # A terminal with no cell its own net's tracks may enter (a pad wholly inside a track keep-out, as
    # under a power pour) cannot get a track; it is left out, as Freerouting leaves it, for the pour
    # to connect after routing. The meta file lists it.
    sealed: dict[str, list] = {}
    keepout_flat = maps.in_keepout.ravel()
    for code in routed:
        c = class_of[code]
        kept_cells, kept_anchors = [], []
        for cells, anchor in zip(net_pads[code], anchors[code]):
            t, k = trace[c].ravel()[cells], core[c].ravel()[cells]
            owner = np.where((t == NOBODY) & (k >= 0), k, t)
            if np.any((owner == FREE) | (owner == code)) or not np.all(keepout_flat[cells]):
                kept_cells.append(cells)  # an unreachable pad outside keep-outs stays: the run reports it
                kept_anchors.append(anchor)
            else:
                sealed.setdefault(str(code), []).append(anchor)
        net_pads[code], anchors[code] = kept_cells, kept_anchors
    routed = [code for code in routed if len(net_pads[code]) >= 2]

    with open(out_path, "wb") as sink:
        sink.write(np.array([*shape, len(classes), len(routed), round(VIA_COST_MM / PITCH), *blobs, *radii], dtype=np.int32).tobytes())
        for grid_map in (*trace, *maps.via, *core, maps.pad_net, *maps.via_site):
            sink.write(grid_map.tobytes())
        for code in routed:
            sink.write(np.array([code, class_of[code], len(net_pads[code])], dtype=np.int32).tobytes())
            for cells in net_pads[code]:
                sink.write(np.array([cells.size], dtype=np.int32).tobytes())
                sink.write(cells.tobytes())
    _save_state(out_path, read, signatures, boxes, terminals, maps, track_band, via_band)
    names = read["names"]
    meta |= {
        "x0": ctx.grid.x0,
        "y0": ctx.grid.y0,
        "pitch": PITCH,
        "shape": [int(v) for v in shape],
        "layer_ids": read["layer_ids"],
        "has_outline": read["has_outline"],
        "classes": [dict(zip(("width", "clearance", "via_diameter", "via_drill"), cls)) for cls in classes],
        "nets": {str(code): {"class": class_of[code], "pads": anchors[code]} for code in routed},
        "pad_count": sum(read["pads_of"][code] for code in routed),
        "net_names": {str(code): names[code] for code in routed},
        "all_net_names": {str(code): name for code, name in names.items()},
        "layer_names": read["layer_names"],
        "fixed_pieces": sum(isinstance(item, _Fixed) for item in items),
        "keepouts": sum(isinstance(item, _Keepout) for item in items),
        "min_width": read["min_width"],
        "sealed": sealed,
    }
    Path(meta_path).write_text(json.dumps(meta))
    print(f"export: {mode}, {time.monotonic() - started:.1f} s", file=sys.stderr)


def _save_state(out_path: str, read: dict, signatures: list[str], boxes: list, terminals: list, maps: _Maps,
                track_band: np.ndarray, via_band: np.ndarray) -> None:
    """What the next export needs to patch from this one (the maps themselves are in the board file)."""
    by_signature = {sig: t for sig, t in zip(signatures, terminals) if t is not None}
    listed = list(by_signature.items())
    cells = np.concatenate([t.cells for _, t in listed]) if listed else np.zeros(0, np.uint32)
    ends = np.cumsum([t.cells.size for _, t in listed]).astype(np.int64)
    np.savez(out_path + ".state.npz", in_keepout=np.packbits(maps.in_keepout), track_band=np.packbits(track_band),
             via_band=np.packbits(via_band), terminal_cells=cells, terminal_ends=ends)
    Path(out_path + ".state.json").write_text(json.dumps({
        "level": read["level"],
        "names": {str(code): name for code, name in read["names"].items()},
        "items": [[sig, list(box) if box else None] for sig, box in zip(signatures, boxes)],
        "terminals": [[sig, int(t.cells.size), t.anchor] for sig, t in listed],
    }))


if __name__ == "__main__":
    command, board_path, out_path, meta_path, options_path, *previous = sys.argv[1:]
    if command != "export":
        raise SystemExit(f"unknown command {command}")
    options = json.loads(Path(options_path).read_text())
    PITCH = float(options.get("pitch", 0.05))
    MARGIN = float(options.get("margin_factor", 0.2)) * PITCH
    NARROW_NETS = frozenset(options.get("narrow", []))
    NARROW_FLOOR_MM = max(NARROW_FLOOR_MM, float(options.get("min_width", 0.0)))
    MIN_WIDTH_MM = float(options.get("min_width", 0.0))
    VIA_COST_MM = float(options.get("via_cost_mm", VIA_COST_MM))
    export(board_path, out_path, meta_path, previous[0] if previous else None)
