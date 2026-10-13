"""The board as the router sees it: the grid, what may stand in each cell, and the nets to connect.

`board.py` writes the file read here; its docstring gives the layout.
"""

from std.algorithm import vectorize
from std.math import ceil, floor
from std.os import SEEK_CUR
from std.sys import simd_width_of, size_of

from buffer import ZeroedBuffer

comptime FREE: Int16 = -1
"""Any net may use the cell."""
comptime NOBODY: Int16 = -2
"""No net may use the cell."""


# ===-----------------------------------------------------------------------===#
# Reading
# ===-----------------------------------------------------------------------===#


def read_values[
    dtype: DType
](file: FileHandle, count: Int) raises -> List[Scalar[dtype]]:
    """Reads `count` values of `dtype`, as stored, from `file`.

    Parameters:
        dtype: The type of the values.

    Args:
        file: The file, at the first value.
        count: How many values to read.

    Returns:
        The values.

    Raises:
        If the file ends first.
    """
    var values = List[Scalar[dtype]](unsafe_uninit_length=count)
    _fill(file, Span(values))
    return values^


def read_grid[
    dtype: DType
](file: FileHandle, count: Int) raises -> ZeroedBuffer[Scalar[dtype]]:
    """Reads `count` values of `dtype` into a per-cell buffer.

    Raises:
        If the file ends first.
    """
    var grid = ZeroedBuffer[Scalar[dtype]](count)
    _fill(
        file,
        Span[Scalar[dtype], MutUntrackedOrigin](
            unsafe_ptr=grid.unsafe_ptr(), length=count
        ),
    )
    return grid^


def _fill[
    dtype: DType, origin: MutOrigin, //
](file: FileHandle, values: Span[Scalar[dtype], origin]) raises:
    var done = 0
    while done < len(values):
        var got = file.read(values[done:])
        if got <= 0:
            raise Error("the file ends early")
        done += got // size_of[dtype]()


# ===-----------------------------------------------------------------------===#
# Offset
# ===-----------------------------------------------------------------------===#


@fieldwise_init
struct Offset(TrivialRegisterPassable):
    """A step from one cell to another in the same layer."""

    var dr: Int
    var dc: Int


def disc(radius: Float64, *, include_edge: Bool) -> List[Offset]:
    """The offsets of the cells whose centres lie within `radius` cells of a cell's centre.

    Args:
        radius: The radius, in cells.
        include_edge: Whether a cell exactly `radius` away counts (a via's blob) or
            not (the keep-out round a track).

    Returns:
        The offsets, row by row.
    """
    var reach = Int(floor(radius)) if include_edge else Int(ceil(radius))
    var limit = radius * radius
    var offsets = List[Offset]()
    for dr in range(-reach, reach + 1):
        for dc in range(-reach, reach + 1):
            var distance = Float64(dr * dr + dc * dc)
            if distance <= limit if include_edge else distance < limit:
                offsets.append(Offset(dr, dc))
    return offsets^


@fieldwise_init
struct Run(TrivialRegisterPassable):
    """One row of a disc: `length` cells from `dc` columns off the centre,
    `dr` rows off it."""

    var dr: Int
    var dc: Int
    var length: Int


def runs(offsets: List[Offset]) -> List[Run]:
    """The offsets as row runs, in the same order, for scanning a row at a
    time."""
    var result = List[Run]()
    for offset in offsets:
        var last = len(result) - 1
        if (
            last >= 0
            and result[last].dr == offset.dr
            and result[last].dc + result[last].length == offset.dc
        ):
            result[last].length += 1
        else:
            result.append(Run(offset.dr, offset.dc, 1))
    return result^


def uncovered(offsets: List[Offset], dr: Int, dc: Int) -> List[Run]:
    """The part of a disc a step of (`dr`, `dc`) uncovers: the offsets `o`
    with `o + (dr, dc)` outside the disc, as row runs in the disc's order.

    A cell's disc, less the disc of the cell one step back, is exactly these
    offsets round the cell.
    """
    var reach = 0
    for offset in offsets:
        reach = max(reach, max(abs(offset.dr), abs(offset.dc)))
    # The disc on a grid with a one-cell margin, so a moved offset can be
    # looked up without a bounds check.
    var side = 2 * reach + 3
    var inside = List[Bool](length=side * side, fill=False)

    @inline(.always)
    def at(dr: Int, dc: Int) {imm} -> Int:
        return (dr + reach + 1) * side + (dc + reach + 1)

    for offset in offsets:
        inside[at(offset.dr, offset.dc)] = True
    var kept = List[Offset]()
    for offset in offsets:
        if not inside[at(offset.dr + dr, offset.dc + dc)]:
            kept.append(offset)
    return runs(kept)


# ===-----------------------------------------------------------------------===#
# Pad and Net
# ===-----------------------------------------------------------------------===#


@fieldwise_init
struct Pad(Copyable):
    """A terminal of a net: its cells, and the rows and columns they span."""

    var cells: List[UInt32]
    var r0: Int
    var r1: Int
    var c0: Int
    var c1: Int

    def gap(self, other: Pad) -> Int:
        """Rows plus columns between the two pads' boxes; 0 if they overlap."""
        return max(0, max(self.r0 - other.r1, other.r0 - self.r1)) + max(
            0, max(self.c0 - other.c1, other.c0 - self.c1)
        )


struct Net(Copyable):
    """A net to connect, and the route it has now."""

    var id: Int
    var cls: Int
    var pads: List[Pad]
    var span: Int
    """Rows plus columns spanned by all its pads."""
    var path: List[UInt32]
    """Centreline cells and via blobs, off the net's own pads."""
    var tree: List[UInt32]
    """Cells its copper may be entered from."""
    var connections: List[List[UInt32]]
    """Each from the newly joined pad back to the tree."""
    var footprint: List[List[UInt32]]
    """Per observing class: cells whose occupancy this net raised."""
    var via_count: Int
    var dead: Bool
    """A pad could not be reached."""
    var blocked_pad: Int
    """A pad no track can reach; found once, remembered."""
    var preset: List[UInt32]
    """From a previous session: centreline cells still standing."""
    var preset_vias: List[UInt32]
    """From a previous session: via positions, in-plane."""
    var kept: Bool
    """The preset is part of the net's current copper."""
    var preset_n: Int
    """`path[:preset_n]` came from the preset."""
    var preset_footprint: List[List[UInt32]]
    """Per observing class, counted in the kept tally."""
    var conflict_rounds: Int
    """Rounds in a row with other nets' new copper on its kept copper."""

    def __init__(
        out self, id: Int, cls: Int, var pads: List[Pad], classes: Int
    ):
        self.id, self.cls = id, cls
        var r0, r1, c0, c1 = Int.MAX, 0, Int.MAX, 0
        for ref pad in pads:
            r0, r1 = min(r0, pad.r0), max(r1, pad.r1)
            c0, c1 = min(c0, pad.c0), max(c1, pad.c1)
        self.span = (r1 - r0) + (c1 - c0)
        self.pads = pads^
        self.path = List[UInt32]()
        self.tree = List[UInt32]()
        self.connections = List[List[UInt32]]()
        self.footprint = List[List[UInt32]](length=classes, fill=List[UInt32]())
        self.via_count, self.dead, self.blocked_pad = 0, False, -1
        self.preset = List[UInt32]()
        self.preset_vias = List[UInt32]()
        self.kept, self.preset_n = False, 0
        self.preset_footprint = List[List[UInt32]]()
        self.conflict_rounds = 0

    def has_preset(self) -> Bool:
        """Whether a previous session's route is offered to this net."""
        return len(self.preset) > 0 or len(self.preset_vias) > 0

    def area(self) -> Int:
        """Cells in the box round all its pads."""
        var r0, r1, c0, c1 = Int.MAX, 0, Int.MAX, 0
        for ref pad in self.pads:
            r0, r1 = min(r0, pad.r0), max(r1, pad.r1)
            c0, c1 = min(c0, pad.c0), max(c1, pad.c1)
        return (r1 - r0 + 1) * (c1 - c0 + 1)

    def forget_footprint(mut self):
        """Forgets which cells this net's copper was counted in, for a copy
        about to be counted afresh."""
        for i in range(len(self.footprint)):
            self.footprint[i].clear()
        for i in range(len(self.preset_footprint)):
            self.preset_footprint[i].clear()

    def clear_route(mut self, *, keep_preset: Bool):
        """Forgets the route; with `keep_preset`, the kept copper is laid again next time.
        """
        self.preset_n = 0
        self.path.clear()
        self.tree.clear()
        self.connections.clear()
        self.via_count = 0
        if not keep_preset:
            self.preset.clear()
            self.preset_vias.clear()
            self.kept = False
            self.blocked_pad = -1


# ===-----------------------------------------------------------------------===#
# Board
# ===-----------------------------------------------------------------------===#


struct Board(Movable):
    """The grid and what may stand in each cell, per net class."""

    var layers: Int
    var rows: Int
    var cols: Int
    var classes: Int
    var via_cost: Int
    """What a via costs, in cells of track."""
    var net_count: Int
    var core: List[ZeroedBuffer[Int16]]
    """Per class: the net whose own pad wholly covers a track centred here."""
    var owner: List[ZeroedBuffer[Int16]]
    """Per class: which net alone may enter a cell."""
    var via_sites: List[ZeroedBuffer[Int16]]
    """Per class: which net alone may drill here, the whole via considered."""
    var via_blob: List[List[Offset]]
    """Per class: the cells a via covers."""
    var keep_out_steps: List[List[List[Run]]]
    """Per laying class and observing class, the disc of cells a track keeps
    clear: per step to a neighbouring cell (see `step_index`), the row runs
    that step uncovers. The step that does not move holds the whole disc,
    which is what a cell uncovers with no neighbour before it."""
    var via_runs: List[List[Run]]
    """`via_blob` as row runs."""

    def __init__(out self, file: FileHandle) raises:
        """Reads the grid from a board file, leaving `file` at the first net.

        Raises:
            If the file ends early.
        """
        var head = read_values[DType.int32](file, 6)
        self.layers = Int(head[0])
        self.rows = Int(head[1])
        self.cols = Int(head[2])
        self.classes = Int(head[3])
        self.net_count = Int(head[4])
        self.via_cost = Int(head[5])
        var k = self.classes
        var blob_radius = read_values[DType.int32](file, k)
        var exclusion = read_values[DType.int32](file, k * k)
        var keep_out = List[List[Offset]]()
        for radius in exclusion:
            keep_out.append(disc(Float64(radius) / 1000.0, include_edge=False))
        self.via_blob = List[List[Offset]]()
        for radius in blob_radius:
            self.via_blob.append(
                disc(Float64(radius) / 1000.0, include_edge=True)
            )
        self.keep_out_steps = List[List[List[Run]]]()
        for ref offsets in keep_out:
            var steps = List[List[Run]]()
            for index in range(9):
                var dr, dc = index // 3 - 1, index % 3 - 1
                if dr == 0 and dc == 0:
                    steps.append(runs(offsets))
                else:
                    steps.append(uncovered(offsets, dr, dc))
            self.keep_out_steps.append(steps^)
        self.via_runs = List[List[Run]]()
        for ref offsets in self.via_blob:
            self.via_runs.append(runs(offsets))
        var plane = self.rows * self.cols
        var n = plane * self.layers
        # Which net alone may enter a cell: read as the trace map, then
        # opened where a net's own pad covers the cell (see `_open_own_pads`).
        self.owner = List[ZeroedBuffer[Int16]]()
        for _ in range(k):
            self.owner.append(read_grid[DType.int16](file, n))
        # The plain via map: the via sites below supersede it.
        _ = file.seek(2 * plane * k, SEEK_CUR)
        self.core = List[ZeroedBuffer[Int16]]()
        for _ in range(k):
            self.core.append(read_grid[DType.int16](file, n))
        # The pad map: the router does not use it.
        _ = file.seek(2 * n, SEEK_CUR)
        self.via_sites = List[ZeroedBuffer[Int16]]()
        for _ in range(k):
            self.via_sites.append(read_grid[DType.int16](file, plane))
        for cls in range(k):
            _open_own_pads(self.owner[cls], self.core[cls])

    @staticmethod
    @inline(.always)
    def step_index(dr: Int, dc: Int) -> Int:
        """Where a step of (`dr`, `dc`), each -1, 0 or 1, is in
        `keep_out_steps`; (0, 0) is the whole disc."""
        return (dr + 1) * 3 + (dc + 1)

    @inline(.always)
    def plane(self) -> Int:
        """Cells in one layer."""
        return self.rows * self.cols

    @inline(.always)
    def cells(self) -> Int:
        """Cells in all layers."""
        return self.plane() * self.layers

    @inline(.always)
    def enterable(self, net: Net, cell: Int) -> Bool:
        """Whether the net's tracks may enter the cell."""
        var who = self.owner[net.cls][cell]
        return who == FREE or Int(who) == net.id


def _open_own_pads(mut trace: ZeroedBuffer[Int16], core: ZeroedBuffer[Int16]):
    """Turns the trace map into the owner map, in place.

    A cell nobody may enter is still open to a net whose own pad would wholly
    cover a track centred there: such a track adds no copper.
    """
    var owner = trace.unsafe_ptr()
    var by_core = core.unsafe_ptr()

    def pick[width: Int](i: Int) {imm}:
        var t = owner.unsafe_offset(i).unsafe_load[width=width]()
        var c = by_core.unsafe_offset(i).unsafe_load[width=width]()
        var covered = t.eq(SIMD[DType.int16, width](NOBODY)) & c.ge(
            SIMD[DType.int16, width](0)
        )
        owner.unsafe_offset(i).unsafe_store(covered.select(c, t))

    vectorize[simd_width_of[DType.int16]()](len(trace), pick)


def read_nets(file: FileHandle, board: Board) raises -> List[Net]:
    """Reads the nets that follow the grid in a board file.

    Raises:
        If the file ends early.
    """
    var plane = board.plane()
    var nets = List[Net](capacity=board.net_count)
    for _ in range(board.net_count):
        var head = read_values[DType.int32](file, 3)
        var pads = List[Pad]()
        for _ in range(Int(head[2])):
            var count = Int(read_values[DType.int32](file, 1)[0])
            var cells = read_values[DType.uint32](file, count)
            var r0, r1, c0, c1 = board.rows, 0, board.cols, 0
            for cell in cells:
                var in_plane = Int(cell) % plane
                var r, c = in_plane // board.cols, in_plane % board.cols
                r0, r1 = min(r0, r), max(r1, r)
                c0, c1 = min(c0, c), max(c1, c)
            pads.append(Pad(cells^, r0, r1, c0, c1))
        nets.append(Net(Int(head[0]), Int(head[1]), pads^, board.classes))
    return nets^


def read_presets(path: String, mut nets: List[Net]) raises:
    """Gives each net the previous session's route still standing for it.

    The file holds, per net, an int32 net id, cell count and via count, then that many
    uint32 cells and uint32 in-plane via positions. A truncated file ends the list.

    Raises:
        If the file cannot be opened.
    """
    with open(path, "r") as file:
        var count: Int
        try:
            count = Int(read_values[DType.int32](file, 1)[0])
        except:
            return
        for _ in range(count):
            try:
                var head = read_values[DType.int32](file, 3)
                var cells = read_values[DType.uint32](file, Int(head[1]))
                var vias = read_values[DType.uint32](file, Int(head[2]))
                for ref net in nets:
                    if net.id == Int(head[0]):
                        net.preset = cells^
                        net.preset_vias = vias^
                        break
            except:
                return
