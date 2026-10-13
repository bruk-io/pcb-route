"""What routed copper costs: who keeps each cell clear, and the prices negotiation builds up."""

from std.bit import count_trailing_zeros
from std.memory import pack_bits

from board import Board, Net, Run
from buffer import ZeroedBuffer, zeroed


# ===-----------------------------------------------------------------------===#
# Congestion
# ===-----------------------------------------------------------------------===#


def _count(
    path: Span[UInt32, _],
    steps: Span[List[Run], _],
    board: Board,
    mut marks: ZeroedBuffer[UInt32],
    stamp: UInt32,
    mut counts: ZeroedBuffer[UInt16],
    mut footprint: List[UInt32],
):
    """Counts each cell within the disc `steps` describes round each path cell
    once in `counts`, and lists it in `footprint`.

    `marks` holds `stamp` for the cells already counted under this stamp.
    When a path cell is next to the one before it in the same layer, every
    cell of the earlier disc is marked, so only `steps`, the part of the disc
    that step uncovers, can hold unmarked cells. Each row is read a vector at
    a time and only its unmarked cells are counted, in ascending order. A
    row's last vector may overlap the one before: by then the overlap is
    marked.
    """
    var plane, rows, cols = board.plane(), board.rows, board.cols
    var marked = marks.unsafe_ptr()

    @inline(.always)
    def scan[width: Int](at: Int) {mut marks, mut counts, mut footprint, imm}:
        var chunk = marked.unsafe_offset(at).unsafe_load[width=width]()
        var unmarked = pack_bits(chunk.ne(SIMD[DType.uint32, width](stamp)))
        while unmarked != 0:
            var near = at + Int(count_trailing_zeros(unmarked))
            marks[near] = stamp
            counts[near] += 1
            footprint.append(UInt32(near))
            unmarked &= unmarked - 1

    var last_base, last_r, last_c = -1, 0, 0
    for cell in path:
        var in_plane = Int(cell) % plane
        var base = Int(cell) - in_plane
        var r, c = in_plane // cols, in_plane % cols
        var step = Board.step_index(0, 0)
        if base == last_base and abs(r - last_r) <= 1 and abs(c - last_c) <= 1:
            step = Board.step_index(r - last_r, c - last_c)
        last_base, last_r, last_c = base, r, c
        for run in steps[step]:
            var rr = r + run.dr
            if rr < 0 or rr >= rows:
                continue
            var row = base + rr * cols
            var first = row + max(0, c + run.dc)
            var end = row + min(cols, c + run.dc + run.length)
            var length = end - first
            if length >= 16:
                var at = first
                while at + 16 <= end:
                    scan[16](at)
                    at += 16
                if at < end:
                    scan[16](end - 16)
            elif length >= 8:
                scan[8](first)
                scan[8](end - 8)
            else:
                for near in range(first, end):
                    if marks[near] != stamp:
                        marks[near] = stamp
                        counts[near] += 1
                        footprint.append(UInt32(near))


@fieldwise_init
struct Conflict(TrivialRegisterPassable):
    """Whether a net shares cells with another net."""

    var shared: Bool
    """It shares at least one cell."""
    var on_kept: Bool
    """A shared cell lies on its kept copper."""


@fieldwise_init
struct Prices(TrivialRegisterPassable):
    """What cells cost a net of one class: the tallies it reads, held by
    pointer for the search's inner loop. Valid while the `Congestion` it came
    from lives and its tallies do not change."""

    var _history: Pointer[Float32, MutUntrackedOrigin]
    var _occupancy: Pointer[UInt16, MutUntrackedOrigin]
    var _kept: Pointer[UInt16, MutUntrackedOrigin]
    var _present: Float64
    var _kept_price: Float64

    @inline(.always)
    def multiples[width: Int](self, cell: Int) -> SIMD[DType.float64, width]:
        """The price of `width` cells from `cell`."""
        comptime F64 = SIMD[DType.float64, width]
        var history = (
            self._history.unsafe_offset(cell)
            .unsafe_load[width=width]()
            .cast[DType.float64]()
        )
        var occupancy = (
            self._occupancy.unsafe_offset(cell)
            .unsafe_load[width=width]()
            .cast[DType.float64]()
        )
        var kept = (
            self._kept.unsafe_offset(cell)
            .unsafe_load[width=width]()
            .cast[DType.float64]()
        )
        return (F64(1.0) + history) * (
            F64(1.0)
            + F64(self._present) * occupancy
            + F64(self._kept_price) * kept
        )

    @inline(.always)
    def multiple(self, cell: Int) -> Float64:
        """The price of a cell."""
        return self.multiples[1](cell)

    def dearest(self, centre: Int, runs: Span[Run, _], cols: Int) -> Float64:
        """The dearest cell in `runs` round `centre`, and never below 1.

        Each run is scanned in vectors; a run's last vector may overlap the one
        before it, which a maximum does not mind. Most vectors have no copper
        on them: there a cell's price is exactly `1 + history`, and since the
        price only grows with each tally, their dearest is `1 + ` their
        largest history, found without the rest of the arithmetic.
        """
        var worst8 = SIMD[DType.float64, 8](1.0)
        var worst4 = SIMD[DType.float64, 4](1.0)
        var worst = 1.0
        var calm8 = SIMD[DType.float32, 8](0.0)
        var calm4 = SIMD[DType.float32, 4](0.0)

        @inline(.always)
        def scan8(at: Int) {mut worst8, mut calm8, self}:
            if self._clear[8](at):
                calm8 = max(calm8, self._history_at[8](at))
            else:
                worst8 = max(worst8, self.multiples[8](at))

        @inline(.always)
        def scan4(at: Int) {mut worst4, mut calm4, self}:
            if self._clear[4](at):
                calm4 = max(calm4, self._history_at[4](at))
            else:
                worst4 = max(worst4, self.multiples[4](at))

        for run in runs:
            var start = centre + run.dr * cols + run.dc
            var length = run.length
            if length >= 8:
                var i = 0
                while i + 8 <= length:
                    scan8(start + i)
                    i += 8
                if i < length:
                    scan8(start + length - 8)
            elif length >= 4:
                scan4(start)
                scan4(start + length - 4)
            else:
                for i in range(length):
                    worst = max(worst, self.multiple(start + i))
        var calm = max(calm8.reduce_max(), calm4.reduce_max())
        return max(
            max(worst, 1.0 + Float64(calm)),
            max(worst8.reduce_max(), worst4.reduce_max()),
        )

    @inline(.always)
    def _clear[width: Int](self, cell: Int) -> Bool:
        """Whether no copper, new or kept, covers the `width` cells from
        `cell`."""
        var occupancy = self._occupancy.unsafe_offset(cell).unsafe_load[
            width=width
        ]()
        var kept = self._kept.unsafe_offset(cell).unsafe_load[width=width]()
        return (occupancy | kept).reduce_or() == 0

    @inline(.always)
    def _history_at[width: Int](self, cell: Int) -> SIMD[DType.float32, width]:
        return self._history.unsafe_offset(cell).unsafe_load[width=width]()


struct Congestion(Movable):
    """Per observing class, how many nets keep each cell clear, and what a cell costs.

    Nets may share cells at a price that grows each round. A net's centreline keeps other
    nets' centrelines out of a disc whose radius depends on both nets' classes; the tally
    for class `c` counts, for a net of class `c` looking at a cell, how many nets' discs
    cover it. Copper kept from a previous session is counted apart: two kept pieces were
    legal together before the edit, so their overlap on this grid is no conflict.
    """

    var occupancy: List[ZeroedBuffer[UInt16]]
    """New copper, per observing class."""
    var kept: List[ZeroedBuffer[UInt16]]
    """Kept copper, per observing class."""
    var history: ZeroedBuffer[Float32]
    """What each cell has cost before."""
    var present: Float64
    """What sharing a cell costs this round."""
    var kept_price: Float64
    """What crossing kept copper costs, per cell, from round 1."""
    var _stamped: ZeroedBuffer[UInt32]
    """The cells counted under each stamp; a stamp per class per lay, so one
    buffer serves every class."""
    var _stamp: UInt32
    var _own: ZeroedBuffer[UInt32]
    var _own_id: UInt32

    def __init__(out self, board: Board):
        var n = board.cells()
        self.occupancy = zeroed[UInt16](board.classes, n)
        self.kept = zeroed[UInt16](board.classes, n)
        self.history = ZeroedBuffer[Float32](n)
        self.present, self.kept_price = 0.0, 5.0
        self._stamped = ZeroedBuffer[UInt32](n)
        self._stamp = 0
        self._own = ZeroedBuffer[UInt32](n)
        self._own_id = 0

    def prices(self, cls: Int) -> Prices:
        """What cells cost a net of class `cls` until the tallies change."""
        return Prices(
            self.history.unsafe_ptr(),
            self.occupancy[cls].unsafe_ptr(),
            self.kept[cls].unsafe_ptr(),
            self.present,
            self.kept_price,
        )

    @inline(.always)
    def is_clear(self, cls: Int, cell: Int) -> Bool:
        """Whether no copper keeps a track of class `cls` off the cell."""
        return self.occupancy[cls][cell] == 0 and self.kept[cls][cell] == 0

    def lay(mut self, mut net: Net, board: Board):
        """Counts the net's copper in every class's tally.

        New copper first, then kept copper where new copper does not already cover it:
        each cell counts the net once, in one of the two tallies.
        """
        if len(net.preset_footprint) != board.classes:
            net.preset_footprint = List[List[UInt32]](
                length=board.classes, fill=List[UInt32]()
            )
        for observer in range(board.classes):
            self._stamp += 1
            var pair = net.cls * board.classes + observer
            ref steps = board.keep_out_steps[pair]
            _count(
                net.path[net.preset_n :],
                Span(steps),
                board,
                self._stamped,
                self._stamp,
                self.occupancy[observer],
                net.footprint[observer],
            )
            _count(
                net.path[: net.preset_n],
                Span(steps),
                board,
                self._stamped,
                self._stamp,
                self.kept[observer],
                net.preset_footprint[observer],
            )

    def take_up(mut self, mut net: Net):
        """Takes the net's copper out of every tally."""
        for observer in range(len(self.occupancy)):
            for cell in net.footprint[observer]:
                self.occupancy[observer][Int(cell)] -= 1
            net.footprint[observer].clear()
            if observer < len(net.preset_footprint):
                for cell in net.preset_footprint[observer]:
                    self.kept[observer][Int(cell)] -= 1
                net.preset_footprint[observer].clear()

    def conflict(mut self, net: Net) -> Conflict:
        """Whether the net shares cells with another net, and whether any of them lie on its
        kept copper (where it is counted in the kept tally unless its new copper covers it).
        """
        self._own_id += 1
        var own = self._own_id
        ref occupancy = self.occupancy[net.cls]
        ref kept = self.kept[net.cls]
        for cell in net.footprint[net.cls]:
            self._own[Int(cell)] = own
        var shared = False
        for i, cell in enumerate(net.path):
            var own_new = self._own[Int(cell)] == own
            var new_others = Int(occupancy[Int(cell)]) - Int(own_new)
            var kept_others = Int(kept[Int(cell)]) - Int(not own_new)
            if i < net.preset_n:
                if new_others > 0:
                    return Conflict(shared=True, on_kept=True)
            elif new_others + kept_others > 0:
                shared = True
        return Conflict(shared=shared, on_kept=False)
