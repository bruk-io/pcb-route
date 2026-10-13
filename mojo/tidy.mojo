"""Tidying a finished route: straight track where the grid left staircases.

An A* path on a grid with diagonal steps has many equally cheap shapes, and the
one it finds often alternates straight and diagonal steps, which becomes many
short pieces of track. Once negotiation is over, each net's connections are
redrawn between their fixed points (ends, vias, junctions with the net's other
connections) as long runs of at most two pieces, one straight and one at 45
degrees, wherever every cell of the new run is clear of all other copper.

Only clear cells are ever used, so tidying cannot add a conflict.
"""

from std.collections import Set

from board import FREE, Board, Net
from congestion import Congestion
from search import Search


def tidy(
    mut nets: List[Net],
    board: Board,
    mut congestion: Congestion,
    mut search: Search,
):
    """Straightens every net's connections, net by net.

    A net in conflict is left as it is: its own cells are not clear.
    """
    for ref net in nets:
        if not net.connections or congestion.conflict(net).shared:
            continue
        congestion.take_up(net)
        var fixed = Set[Int]()
        for ref walked in net.connections:
            fixed.add(Int(walked[0]))
            fixed.add(Int(walked[len(walked) - 1]))
        for i in range(len(net.connections)):
            net.connections[i] = _straighten(
                net.connections[i], fixed, net, board, congestion
            )
        search.retrace(net, board)
        congestion.lay(net, board)


def _straighten(
    walked: List[UInt32],
    fixed: Set[Int],
    net: Net,
    board: Board,
    congestion: Congestion,
) -> List[UInt32]:
    """The connection with each stretch between fixed cells redrawn.

    A stretch is a run of cells in one layer between two fixed cells: the
    connection's ends, a junction, or either side of a via.
    """
    var plane = board.plane()
    var tidied = List[UInt32](capacity=len(walked))
    var start = 0
    for i in range(1, len(walked)):
        var cell = Int(walked[i])
        var layer_change = Int(walked[i - 1]) % plane == cell % plane
        var ends_here = (
            i + 1 == len(walked)
            or cell in fixed
            or Int(walked[i + 1]) % plane == cell % plane
        )
        if layer_change:
            tidied.append(walked[i - 1])
            start = i
        elif ends_here:
            _redraw(walked, start, i, tidied, net, board, congestion)
            start = i
    tidied.append(walked[len(walked) - 1])
    return tidied^


def _redraw(
    walked: List[UInt32],
    first: Int,
    last: Int,
    mut tidied: List[UInt32],
    net: Net,
    board: Board,
    congestion: Congestion,
):
    """Appends the stretch `walked[first:last]`, redrawn; `walked[last]` is
    the next stretch's first cell.

    From each cell, the stretch jumps to the furthest later cell that a clear
    run of at most two pieces reaches, trying further cells until one fails.
    """
    var at = first
    while at < last:
        tidied.append(walked[at])
        var best = at + 1
        var best_run = List[UInt32]()
        for target in range(at + 2, last + 1):
            var run = _clear_run(
                Int(walked[at]), Int(walked[target]), net, board, congestion
            )
            if not run:
                break
            best = target
            best_run = run.take()
        for cell in best_run:  # the run's cells between `at` and `best`
            tidied.append(cell)
        at = best


def _clear_run(
    start: Int, end: Int, net: Net, board: Board, congestion: Congestion
) -> Optional[List[UInt32]]:
    """The cells strictly between `start` and `end` on a clear run of at most
    two pieces, diagonal first or straight first, or None if neither is clear.
    """
    var run = _run(start, end, True, net, board, congestion)
    if run:
        return run^
    return _run(start, end, False, net, board, congestion)


def _run(
    start: Int,
    end: Int,
    diagonal_first: Bool,
    net: Net,
    board: Board,
    congestion: Congestion,
) -> Optional[List[UInt32]]:
    var plane, cols = board.plane(), board.cols
    var base = start - start % plane
    var r, c = (start % plane) // cols, (start % plane) % cols
    var r1, c1 = (end % plane) // cols, (end % plane) % cols
    var dr, dc = r1 - r, c1 - c
    var sr, sc = (1 if dr > 0 else -1 if dr < 0 else 0), (
        1 if dc > 0 else -1 if dc < 0 else 0
    )
    var diagonals = min(abs(dr), abs(dc))
    var straights = max(abs(dr), abs(dc)) - diagonals
    var straight_r, straight_c = (sr, 0) if abs(dr) > abs(dc) else (0, sc)
    ref owner = board.owner[net.cls]

    @inline(.always)
    def passable(rr: Int, cc: Int) {imm} -> Bool:
        var who = owner[base + rr * cols + cc]
        return who == FREE or Int(who) == net.id

    var cells = List[UInt32]()
    for k in range(diagonals + straights):
        var diagonal = (k < diagonals) if diagonal_first else (k >= straights)
        var step_r, step_c = (sr, sc) if diagonal else (straight_r, straight_c)
        # A diagonal step brushes past the two cells beside it: as the
        # search, it is allowed only where both are passable.
        if diagonal and not (
            passable(r + step_r, c) and passable(r, c + step_c)
        ):
            return None
        r, c = r + step_r, c + step_c
        var cell = base + r * cols + c
        if not passable(r, c) or not congestion.is_clear(net.cls, cell):
            return None
        if cell != end:
            cells.append(UInt32(cell))
    return cells^
