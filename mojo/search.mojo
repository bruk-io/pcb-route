"""Routing one net: a bucket-queue A* from the net's copper to each pad in turn."""

from std.math import sqrt
from std.time import perf_counter_ns

from board import FREE, Board, Net, Run
from buffer import ZeroedBuffer
from congestion import Congestion, Prices
from cost import SCALE, llround, step_units


comptime _VIA_EVENT = UInt32(1) << 31
"""Marks a queue entry as a lazily priced via at a cell, not the cell itself;
cells stay below 2^31."""


@fieldwise_init
struct _Node(TrivialRegisterPassable):
    """A cell's search state, side by side so that a visit reads one cache
    line instead of three."""

    var best: Int64
    """The cheapest cost found to the cell, in this search."""
    var seen: UInt32
    """The search that last reached the cell; `best` is stale otherwise."""
    var closed: UInt32
    """The search that last expanded the cell."""


struct Search(Movable):
    """The search's working state, reused from one connection to the next.

    Each array marks a cell with the id of the search (or path) that last touched it, so
    nothing has to be cleared between searches.
    """

    var weight_tenths: Int
    """The estimate is multiplied by this over ten; above 10 trades path cost for speed."""
    var lazy_vias: Bool
    """Price a via site only when the cheapest via there could be next on
    the cheapest path, not when its cell is expanded. The paths found are
    as cheap; ties may go another way, so off, the search is the C++
    router's."""
    var expansions: Int
    """Cells expanded, all searches together."""
    var search_ns: Int
    """Time spent connecting pads, all nets together."""
    var _diagonal: Int
    var _nodes: ZeroedBuffer[_Node]
    var _is_target: ZeroedBuffer[UInt32]
    var _parent: ZeroedBuffer[UInt32]
    var _in_path: ZeroedBuffer[UInt32]
    var _via_seen: ZeroedBuffer[UInt32]
    """Per site: the search that last priced a via there."""
    var _via_price: ZeroedBuffer[Int64]
    var _via_units: Int
    """What a via costs before the price of the cells it covers."""
    var _levels: List[List[UInt32]]
    """The bucket queue: cells by estimated total cost."""
    var _level: Int
    var _top_level: Int
    var _search_id: UInt32
    var _path_id: UInt32

    def __init__(
        out self, board: Board, via_factor: Float64, weight_tenths: Int
    ):
        var n, plane = board.cells(), board.plane()
        self.weight_tenths = weight_tenths
        self.lazy_vias = False
        self.expansions, self.search_ns = 0, 0
        self._diagonal = llround(Float64(SCALE) * sqrt(2.0))
        self._nodes = ZeroedBuffer[_Node](n)
        self._is_target = ZeroedBuffer[UInt32](n)
        self._parent = ZeroedBuffer[UInt32](n)
        self._in_path = ZeroedBuffer[UInt32](n)
        self._via_seen = ZeroedBuffer[UInt32](plane)
        self._via_price = ZeroedBuffer[Int64](plane)
        self._via_units = max(1, llround(Float64(board.via_cost) * via_factor))
        self._levels = List[List[UInt32]]()
        self._level, self._top_level = 0, 0
        self._search_id, self._path_id = 0, 0

    # ===-------------------------------------------------------------------===#
    # Routing a net
    # ===-------------------------------------------------------------------===#

    def route(mut self, mut net: Net, board: Board, mut congestion: Congestion):
        """Connects every pad of the net, nearest first, then counts its copper.

        A net offered a previous session's route lays it without searching and connects
        only the pads it does not reach. A pad no track can reach is left out: one
        unreachable pad must not cost the net all its other connections.
        """
        self._path_id += 1
        var joined = List[Bool](length=len(net.pads), fill=False)
        if net.has_preset():
            self._lay_preset(net, board, joined)
        if not any(joined):
            joined[0] = True
            for cell in net.pads[0].cells:
                if board.enterable(net, Int(cell)):
                    net.tree.append(cell)
        var start = perf_counter_ns()
        for _ in range(joined.count(True), len(net.pads)):
            var pick, nearest = -1, Int.MAX
            for i in range(len(net.pads)):
                if joined[i]:
                    continue
                for j in range(len(net.pads)):
                    if joined[j]:
                        var gap = net.pads[i].gap(net.pads[j])
                        if gap < nearest:
                            pick, nearest = i, gap
            var reached = pick != net.blocked_pad and self.connect(
                net, pick, board, congestion
            )
            if not reached:
                net.blocked_pad = pick
                net.dead = True
            joined[pick] = True
        self.search_ns += perf_counter_ns() - start
        congestion.lay(net, board)

    def _lay_preset(
        mut self, mut net: Net, board: Board, mut joined: List[Bool]
    ):
        """Lays the previous session's route as the net's first copper, and marks the pads it reaches.
        """
        var plane = board.plane()
        self._search_id += 1
        var mark = self._search_id
        for cell in net.preset:
            self._add_to_path(net, Int(cell), board)
            net.tree.append(cell)
            self._node(Int(cell)).seen = mark
        for site in net.preset_vias:
            var r, c = Int(site) // board.cols, Int(site) % board.cols
            net.via_count += 1
            for layer in range(board.layers):
                var cell = layer * plane + Int(site)
                net.tree.append(UInt32(cell))
                self._node(cell).seen = mark
                for offset in board.via_blob[net.cls]:
                    var rr, cc = r + offset.dr, c + offset.dc
                    if (
                        rr >= 0
                        and rr < board.rows
                        and cc >= 0
                        and cc < board.cols
                    ):
                        self._add_to_path(
                            net, layer * plane + rr * board.cols + cc, board
                        )
        for i in range(len(net.pads)):
            for cell in net.pads[i].cells:
                if self._node(Int(cell)).seen == mark:
                    joined[i] = True
                    break
        net.kept = True
        net.preset_n = len(net.path)
        for i in range(len(net.pads)):
            if joined[i]:
                for cell in net.pads[i].cells:
                    if board.enterable(net, Int(cell)):
                        net.tree.append(cell)

    # ===-------------------------------------------------------------------===#
    # Connecting one pad
    # ===-------------------------------------------------------------------===#

    def connect(
        mut self,
        mut net: Net,
        target: Int,
        board: Board,
        congestion: Congestion,
    ) -> Bool:
        """Finds the cheapest path from the net's copper to pad `target`, and adds it.

        Moves are straight, diagonal (only when both cells it brushes past are open) or a
        via to every other layer. Returns whether the pad was reached.
        """
        var plane, rows, cols = board.plane(), board.rows, board.cols
        var cls, id = net.cls, net.id
        var t_r0, t_r1 = net.pads[target].r0, net.pads[target].r1
        var t_c0, t_c1 = net.pads[target].c0, net.pads[target].c1
        var diagonal, weight = self._diagonal, self.weight_tenths
        ref owner = board.owner[cls]
        ref sites = board.via_sites[cls]
        var prices = congestion.prices(cls)
        var blob = Span(board.via_runs[cls])
        self._search_id += 1
        var search = self._search_id
        for cell in net.pads[target].cells:
            self._is_target[Int(cell)] = search

        @inline(.always)
        def estimate(r: Int, c: Int) {imm} -> Int:
            var dr = max(0, max(t_r0 - r, r - t_r1))
            var dc = max(0, max(t_c0 - c, c - t_c1))
            var near, far = min(dr, dc), max(dr, dc)
            # Never negative, so unsigned division gives the same quotient,
            # with fewer instructions.
            return Int(
                UInt((diagonal * near + SCALE * (far - near)) * weight)
                // UInt(10)
            )

        @inline(.always)
        def passable(cell: Int) {imm} -> Bool:
            var who = owner[cell]
            return who == FREE or Int(who) == id

        self._level, self._top_level = 0, 0
        for cell in net.tree:
            var at = Int(cell)
            var _, r, c = _place(at, plane, cols)
            self._node(at).seen, self._node(at).best = search, 0
            self._parent[at] = cell
            self._push(at, estimate(r, c))
        var reached: Optional[Int] = None
        while True:
            while self._level < self._top_level:
                if self._levels[self._level]:
                    break
                self._level += 1
            if self._level >= self._top_level:
                break
            var entry = self._levels[self._level].pop()
            if entry & _VIA_EVENT:  # a lazily priced via's turn has come
                var at = Int(entry & ~_VIA_EVENT)
                var site, r, c = _place(at, plane, cols)
                var hop = Int(self._node(at).best) + self._via_cost(
                    site, board, prices, blob
                )
                var at_estimate = estimate(r, c)
                for other in range(board.layers):
                    var above = other * plane + site
                    if above != at and self._improves(above, hop, at):
                        self._push(above, hop + at_estimate)
                continue
            var cell = Int(entry)
            ref node = self._node(cell)
            if node.closed == search:
                continue
            var in_plane, r, c = _place(cell, plane, cols)
            var here = Int(node.best)
            var here_estimate = estimate(r, c)
            if here + here_estimate != self._level:
                continue
            node.closed = search
            self.expansions += 1
            if self._is_target[cell] == search:
                reached = cell
                break

            @inline(.always)
            def step(
                neighbour: Int, nr: Int, nc: Int, base: Int
            ) {mut self, imm}:
                var cost = here + step_units(prices.multiple(neighbour), base)
                if self._improves(neighbour, cost, cell):
                    self._push(neighbour, cost + estimate(nr, nc))

            var east = c + 1 < cols and passable(cell + 1)
            var west = c > 0 and passable(cell - 1)
            var south = r + 1 < rows and passable(cell + cols)
            var north = r > 0 and passable(cell - cols)
            if east:
                step(cell + 1, r, c + 1, SCALE)
            if west:
                step(cell - 1, r, c - 1, SCALE)
            if south:
                step(cell + cols, r + 1, c, SCALE)
            if north:
                step(cell - cols, r - 1, c, SCALE)
            if south and east and passable(cell + cols + 1):
                step(cell + cols + 1, r + 1, c + 1, diagonal)
            if south and west and passable(cell + cols - 1):
                step(cell + cols - 1, r + 1, c - 1, diagonal)
            if north and east and passable(cell - cols + 1):
                step(cell - cols + 1, r - 1, c + 1, diagonal)
            if north and west and passable(cell - cols - 1):
                step(cell - cols - 1, r - 1, c - 1, diagonal)
            var drill = sites[in_plane]
            if (
                self.lazy_vias
                and board.layers > 1
                and (drill == FREE or Int(drill) == id)
            ):
                # Queued at the cheapest a via can be: a price multiple of 1.
                self._push_entry(
                    UInt32(cell) | _VIA_EVENT,
                    here + self._via_units * SCALE + here_estimate,
                )
            elif board.layers > 1 and (drill == FREE or Int(drill) == id):
                var hop = here + self._via_cost(in_plane, board, prices, blob)
                for other in range(board.layers):
                    var above = other * plane + in_plane
                    if above != cell and self._improves(above, hop, cell):
                        self._push(above, hop + here_estimate)
        for level in range(self._level, self._top_level):
            self._levels[level].clear()
        if not reached:
            return False
        self._walk_back(net, reached.value(), board)
        for cell in net.pads[target].cells:
            if passable(Int(cell)):
                net.tree.append(cell)
        return True

    def _walk_back(mut self, mut net: Net, reached: Int, board: Board):
        """Adds the path the search found, from the pad back to the net's copper.
        """
        var plane, cols = board.plane(), board.cols
        var walked = List[UInt32]()
        var cell = reached
        while True:
            walked.append(UInt32(cell))
            net.tree.append(UInt32(cell))
            self._add_to_path(net, cell, board)
            var came_from = Int(self._parent[cell])
            if came_from == cell:
                break
            # A layer change: a via and its blob.
            if came_from % plane == cell % plane:
                var in_plane = cell % plane
                var r, c = in_plane // cols, in_plane % cols
                net.via_count += 1
                for layer in range(board.layers):
                    net.tree.append(UInt32(layer * plane + in_plane))
                    for offset in board.via_blob[net.cls]:
                        self._add_to_path(
                            net,
                            layer * plane
                            + (r + offset.dr) * cols
                            + (c + offset.dc),
                            board,
                        )
            cell = came_from
        net.connections.append(walked^)

    def set_via_factor(mut self, board: Board, via_factor: Float64):
        """Makes vias cost `via_factor` times the board's via cost from now on.
        """
        self._via_units = max(1, llround(Float64(board.via_cost) * via_factor))

    @inline(.always)
    def _via_cost(
        mut self, site: Int, board: Board, prices: Prices, blob: Span[Run, _]
    ) -> Int:
        """What a via at `site` costs in this search: its price multiple is
        the dearest cell its blob covers, on any layer. Priced once per
        search."""
        if self._via_seen[site] != self._search_id:
            self._via_seen[site] = self._search_id
            var plane = board.plane()
            var worst = 0.0
            for layer in range(board.layers):
                worst = max(
                    worst,
                    prices.dearest(layer * plane + site, blob, board.cols),
                )
            self._via_price[site] = Int64(
                self._via_units * step_units(worst, SCALE)
            )
        return Int(self._via_price[site])

    def retrace(mut self, mut net: Net, board: Board):
        """Rebuilds the net's path and via count from its kept copper and its
        connections, as routing them built them, after the connections
        changed. The tree is left as it was: only routing reads it.
        """
        var plane, cols = board.plane(), board.cols
        self._path_id += 1
        var kept_cells = net.preset_n
        net.path.shrink(kept_cells)
        for cell in net.path:
            self._in_path[Int(cell)] = self._path_id
        net.via_count = len(net.preset_vias) if net.kept else 0
        for ref walked in net.connections:
            for i in range(len(walked)):
                var cell = Int(walked[i])
                self._add_to_path(net, cell, board)
                if (
                    i + 1 < len(walked)
                    and Int(walked[i + 1]) % plane == cell % plane
                ):
                    var in_plane = cell % plane
                    var r, c = in_plane // cols, in_plane % cols
                    net.via_count += 1
                    for layer in range(board.layers):
                        for offset in board.via_blob[net.cls]:
                            self._add_to_path(
                                net,
                                layer * plane
                                + (r + offset.dr) * cols
                                + (c + offset.dc),
                                board,
                            )

    # ===-------------------------------------------------------------------===#
    # Helpers
    # ===-------------------------------------------------------------------===#

    @inline(.always)
    def _improves(mut self, cell: Int, cost: Int, came_from: Int) -> Bool:
        """Records `cost` as the cheapest way to `cell` unless it is already reached as cheaply.
        """
        ref node = self._node(cell)
        if node.seen == self._search_id and Int(node.best) <= cost:
            return False
        node.seen, node.best = self._search_id, Int64(cost)
        self._parent[cell] = UInt32(came_from)
        return True

    @inline(.always)
    def _node(self, cell: Int) -> ref[MutUntrackedOrigin] _Node:
        # A mutable reference from a read-only `self`, so the A* loop can hold
        # a node while it pushes. Safe because `_nodes` never reallocates and
        # the loop never writes the node it holds through another path.
        return self._nodes.unsafe_ptr()[unsafe_offset=cell]

    @inline(.always)
    def _push(mut self, cell: Int, estimate: Int):
        self._push_entry(UInt32(cell), estimate)

    @inline(.always)
    def _push_entry(mut self, entry: UInt32, estimate: Int):
        if estimate >= len(self._levels):
            self._levels.resize(estimate * 2 + 1024, List[UInt32]())
        self._levels[estimate].append(entry)
        self._top_level = max(self._top_level, estimate + 1)
        # A weighted estimate can fall below the level being emptied.
        self._level = min(self._level, estimate)

    @inline(.always)
    def _add_to_path(mut self, mut net: Net, cell: Int, board: Board):
        """Adds a cell to the net's path once, unless the net's own pad already covers it.
        """
        if (
            Int(board.core[net.cls][cell]) == net.id
            or self._in_path[cell] == self._path_id
        ):
            return
        self._in_path[cell] = self._path_id
        net.path.append(UInt32(cell))


@inline(.always)
def _place(cell: Int, plane: Int, cols: Int) -> Tuple[Int, Int, Int]:
    """The cell's position in its layer, and its row and column.

    Cells fit in 32 bits, and 32-bit unsigned division is several times
    faster than 64-bit signed division.
    """
    var at = UInt32(cell)
    var in_plane = at % UInt32(plane)
    var r = in_plane // UInt32(cols)
    return Int(in_plane), Int(r), Int(in_plane - r * UInt32(cols))
