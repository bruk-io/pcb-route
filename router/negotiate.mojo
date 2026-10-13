"""Negotiated-congestion router: routes every net on the grid `board.py` wrote, negotiates until
no two nets share a cell, takes vias out in via passes, straightens the result and writes it.

    negotiate <board> <label> <order> <seed> <present_start> <present_growth>
              <history_step> <via_factor> [dump [weight_tenths]]

`order` is span_asc, span_desc, pads_desc, area_desc or random (a Fisher-Yates shuffle over
mt19937). A non-zero seed also jitters every cell's starting price.

Environment: NEGOTIATE_PRESET (the previous route still standing), NEGOTIATE_KEPT_OUT (where to
list the nets that kept it), NEGOTIATE_KEPT_PRICE, NEGOTIATE_MAX_ROUNDS, NEGOTIATE_PATIENCE,
NEGOTIATE_WARMUP, NEGOTIATE_SECONDS, NEGOTIATE_VIA_PASS (the via factor for via passes; none
when unset), NEGOTIATE_VIA_PASSES, NEGOTIATE_VIA_PASS_ROUNDS, NEGOTIATE_ONLY and
NEGOTIATE_TRACE.
"""

from std.collections import Set
from std.os import getenv
from std.sys import argv, exit, stderr
from std.time import perf_counter_ns
from std.utils.numerics import isfinite

from board import Board, Net, read_nets, read_presets
from congestion import Congestion
from cost import llround
from mt19937 import MersenneTwister
from search import Search
from stop import StopSignals
from tidy import tidy

comptime MAX_ROUNDS = 60
comptime PRESENT_LIMIT = 1e6


# ===-----------------------------------------------------------------------===#
# Settings
# ===-----------------------------------------------------------------------===#


@fieldwise_init
struct Schedule(TrivialRegisterPassable):
    """How the price of sharing a cell grows from round to round."""

    var present_start: Float64
    var present_growth: Float64
    var history_step: Float32


struct Settings(Movable):
    """What the environment asks of a run; unset or empty means the default."""

    var max_rounds: Int
    var patience: Int
    """Stop once the count of nets in conflict has not fallen for this many rounds."""
    var warmup: Int
    """Never stop for lack of progress before this round."""
    var seconds: Float64
    """Start no round after this long."""
    var kept_price: Float64
    var preset: String
    var kept_out: String
    var only: String
    var trace: Bool
    var via_pass: Float64
    """The via factor for via passes once negotiation has settled; 0 for
    none."""
    var via_pass_rounds: Int
    """The most rounds one via pass may take to settle."""
    var via_passes: Int
    """The most via passes; they stop once one does not help."""

    def __init__(out self) raises:
        self.max_rounds = max(
            1, _number(getenv("NEGOTIATE_MAX_ROUNDS"), MAX_ROUNDS)
        )
        self.patience = _number(getenv("NEGOTIATE_PATIENCE"), 0)
        self.warmup = _number(getenv("NEGOTIATE_WARMUP"), 0)
        self.seconds = _real(getenv("NEGOTIATE_SECONDS"), 0.0)
        self.kept_price = _real(getenv("NEGOTIATE_KEPT_PRICE"), 5.0)
        self.preset = getenv("NEGOTIATE_PRESET")
        self.kept_out = getenv("NEGOTIATE_KEPT_OUT")
        self.only = getenv("NEGOTIATE_ONLY")
        self.trace = getenv("NEGOTIATE_TRACE") != ""
        self.via_pass = _real(getenv("NEGOTIATE_VIA_PASS"), 0.0)
        self.via_pass_rounds = _number(getenv("NEGOTIATE_VIA_PASS_ROUNDS"), 15)
        self.via_passes = _number(getenv("NEGOTIATE_VIA_PASSES"), 1)


def _number(text: String, default: Int) raises -> Int:
    return atol(text) if text else default


def _real(text: String, default: Float64) raises -> Float64:
    return atof(text) if text else default


# ===-----------------------------------------------------------------------===#
# Net order
# ===-----------------------------------------------------------------------===#


@fieldwise_init
struct Order(Equatable, TrivialRegisterPassable):
    """The order nets are first routed in."""

    var _value: Int

    comptime span_asc = Order(0)
    """Shortest first, by the rows plus columns their pads span."""
    comptime span_desc = Order(1)
    """Longest first."""
    comptime pads_desc = Order(2)
    """Most pads first."""
    comptime area_desc = Order(3)
    """Largest box round their pads first."""
    comptime random = Order(4)
    """Shuffled by the seed."""

    @staticmethod
    def parse(name: String) raises -> Order:
        """The order called `name` on the command line.

        Raises:
            If no order has that name.
        """
        if name == "span_asc":
            return .span_asc
        if name == "span_desc":
            return .span_desc
        if name == "pads_desc":
            return .pads_desc
        if name == "area_desc":
            return .area_desc
        if name == "random":
            return .random
        raise Error("unknown order " + name)

    def key(self, net: Net) -> Int:
        """Where the net comes: nets are routed in ascending order of key, ties
        in their file order."""
        if self == .span_asc:
            return net.span
        if self == .span_desc:
            return -net.span
        if self == .pads_desc:
            return -len(net.pads)
        return -net.area()


def net_order(nets: List[Net], order: Order, seed: UInt32) -> List[Int]:
    """The order to route nets in, as indices into `nets`."""
    var indices = List[Int](capacity=len(nets))
    for i in range(len(nets)):
        indices.append(i)
    if order == .random:
        var random = MersenneTwister(seed)
        for i in range(len(indices) - 1, 0, -1):
            indices.swap_elements(i, Int(random.next_u32()) % (i + 1))
        return indices^
    var keys = List[Int](capacity=len(nets))
    for ref net in nets:
        keys.append(order.key(net))

    def by_key(a: Int, b: Int) {imm} -> Bool:
        return keys[a] < keys[b]

    sort[stable=True](Span(indices), by_key)
    return indices^


# ===-----------------------------------------------------------------------===#
# Output
# ===-----------------------------------------------------------------------===#


def _append(mut buffer: List[Byte], value: Int):
    var bytes = Int32(value).as_bytes[big_endian=False]()
    buffer.extend(Span(bytes))


def write_dump(
    path: String, nets: List[Net], mut congestion: Congestion
) raises:
    """Writes each net's connections as cell paths, for `ses.py`.

    The file holds an int32 net count, then per net its id, whether a pad was left
    unreached, whether it is in conflict and its connection count, then per connection a
    cell count and the cells (all int32).

    Raises:
        If the file cannot be written.
    """
    var out = List[Byte]()
    _append(out, len(nets))
    for ref net in nets:
        _append(out, net.id)
        _append(out, Int(net.dead))
        _append(out, Int(congestion.conflict(net).shared))
        _append(out, len(net.connections))
        for ref walked in net.connections:
            _append(out, len(walked))
            for cell in walked:
                _append(out, Int(cell))
    with open(path, "w") as file:
        file.write_bytes(out)


def write_kept(path: String, nets: List[Net]):
    """Lists the nets whose previous route still stands, one id per line.

    A list it cannot write does not cost the result.
    """
    var text = String()
    for ref net in nets:
        if net.kept:
            text += String(net.id, "\n")
    try:
        with open(path, "w") as file:
            file.write(text)
    except:
        pass


def _fixed3(value: Float64) -> String:
    """`value` with three decimals, as printf's `%.3f`."""
    var thousandths = llround(value * 1000.0)
    var fraction = String(thousandths % 1000).ascii_rjust(3, "0")
    return String(thousandths // 1000, ".", fraction)


def _negotiate(
    mut nets: List[Net],
    order: List[Int],
    board: Board,
    mut congestion: Congestion,
    mut search: Search,
    schedule: Schedule,
    settings: Settings,
    mut stop: StopSignals,
    begin: Int,
    first_round: Int,
    max_rounds: Int,
) -> Tuple[Int, Int]:
    """Negotiation rounds from `first_round`: find the nets in conflict, make
    shared cells dearer, route those nets again; until none is in conflict,
    progress stalls, or a limit is reached. Returns the last round and how
    many nets are in conflict after it.
    """
    var rounds = first_round
    var in_conflict: Int
    var fewest, last_gain = Int.MAX, 0
    while True:
        var redo = List[Int]()
        in_conflict = 0
        for i in order:
            var conflict = congestion.conflict(nets[i])
            nets[i].conflict_rounds = (
                nets[i].conflict_rounds + 1 if conflict.on_kept else 0
            )
            if conflict.shared:
                in_conflict += 1
                redo.append(i)
        var seconds = Float64(perf_counter_ns() - begin) / 1e9
        if settings.trace:
            var dead = 0
            for ref net in nets:
                dead += Int(net.dead)
            print(
                "trace",
                rounds,
                in_conflict,
                dead,
                search.expansions,
                seconds,
                sep=",",
                file=stderr,
            )
        if in_conflict < fewest:
            fewest, last_gain = in_conflict, rounds
        var stalled = (
            settings.patience > 0
            and rounds >= settings.warmup
            and rounds - last_gain >= settings.patience
        )
        if (
            stop.requested()
            or in_conflict == 0
            or rounds >= max_rounds
            or stalled
        ):
            break
        if settings.seconds > 0 and seconds > settings.seconds:
            break
        # Cells still shared get dearer for good.
        for i in redo:
            ref net = nets[i]
            ref occupancy = congestion.occupancy[net.cls]
            ref kept = congestion.kept[net.cls]
            for cell in net.path:
                if Int(occupancy[Int(cell)]) + Int(kept[Int(cell)]) > 1:
                    congestion.history[Int(cell)] += schedule.history_step
        if congestion.present == 0:
            congestion.present = schedule.present_start
        else:
            congestion.present = min(
                PRESENT_LIMIT, congestion.present * schedule.present_growth
            )
        for i in redo:
            if stop.requested():
                break  # the nets not yet taken up keep the copper they have
            # A kept net reroutes only its new connections, unless other nets' new copper has sat
            # on its kept copper for more than three rounds: then it is routed afresh.
            ref net = nets[i]
            congestion.take_up(net)
            net.clear_route(keep_preset=net.kept and net.conflict_rounds <= 3)
            net.dead = False
            search.route(net, board, congestion)
        rounds += 1

    return rounds, in_conflict


def _vias(nets: List[Net]) -> Int:
    var total = 0
    for ref net in nets:
        total += net.via_count
    return total


def _connection_vias(net: Net, board: Board) -> Int:
    """Vias in the net's new connections, not its kept copper."""
    var plane, total = board.plane(), 0
    for ref walked in net.connections:
        for k in range(1, len(walked)):
            if Int(walked[k]) % plane == Int(walked[k - 1]) % plane:
                total += 1
    return total


def _fewer_vias(
    mut nets: List[Net],
    order: List[Int],
    board: Board,
    mut congestion: Congestion,
    mut search: Search,
    schedule: Schedule,
    settings: Settings,
    mut stop: StopSignals,
    begin: Int,
    rounds: Int,
) -> Int:
    """One via pass on a settled board: routes the nets whose new connections
    have vias again, at the search's (dearer) via cost, then negotiates until
    the board settles again. Returns the last round; if the board does not
    settle, ends with no fewer vias, or is stopped (a stop signal, or the
    time budget), the nets and their tallies are put back as they were and
    `rounds` is returned; the prices it raised stay raised.
    """
    var before = _vias(nets)
    var saved = nets.copy()
    var out_of_time = False
    for i in order:
        ref net = nets[i]
        if _connection_vias(net, board) == 0:
            continue
        var seconds = Float64(perf_counter_ns() - begin) / 1e9
        if stop.requested() or (
            settings.seconds > 0 and seconds > settings.seconds
        ):
            out_of_time = True
            break
        congestion.take_up(net)
        net.clear_route(keep_preset=net.kept)
        net.dead = False
        search.route(net, board, congestion)
    var last, unsettled = rounds, 1
    if not out_of_time:
        last, unsettled = _negotiate(
            nets,
            order,
            board,
            congestion,
            search,
            schedule,
            settings,
            stop,
            begin,
            rounds + 1,
            rounds + settings.via_pass_rounds,
        )
    if settings.trace:
        print("via pass", before, _vias(nets), unsettled, sep=",", file=stderr)
    if unsettled == 0 and _vias(nets) < before and not stop.requested():
        return last
    for ref net in nets:
        congestion.take_up(net)
    nets = saved^
    for ref net in nets:
        net.forget_footprint()
        congestion.lay(net, board)
    return rounds


# ===-----------------------------------------------------------------------===#
# main
# ===-----------------------------------------------------------------------===#


def main() raises:
    var args = argv()
    if len(args) < 9:
        print(
            (
                "usage: negotiate <board> <label> <order> <seed>"
                " <present_start> <present_growth> <history_step> <via_factor>"
                " [dump [weight_tenths]]"
            ),
            file=stderr,
        )
        exit(1)
    # Read as unsigned 32 bits, so a negative seed still jitters the prices.
    var seed = UInt32(atol(args[4]) & 0xFFFFFFFF)
    var schedule = Schedule(
        atof(args[5]), atof(args[6]), Float32(atof(args[7]))
    )
    var settings = Settings()
    var order_kind = Order.parse(String(args[3]))
    var via_factor = atof(args[8])
    var weight_tenths = atol(args[10]) if len(args) > 10 else 10
    # The prices are only defined for finite numbers: an infinite or NaN one
    # makes some cell's price NaN, and then no route is the cheapest.
    for value in [
        schedule.present_start,
        schedule.present_growth,
        Float64(schedule.history_step),
        via_factor,
        settings.kept_price,
    ]:
        if not isfinite(value):
            print("every price setting must be a finite number", file=stderr)
            exit(1)
    if weight_tenths < 0:
        print("weight_tenths must not be negative", file=stderr)
        exit(1)

    var board: Board
    var nets: List[Net]
    with open(String(args[1]), "r") as file:
        board = Board(file)
        nets = read_nets(file, board)
    var order = net_order(nets, order_kind, seed)
    if board.cells() >= 1 << 31:  # the search flags queue entries in bit 31
        print("the grid has more than 2^31 cells", file=stderr)
        exit(1)
    var search = Search(board, via_factor, weight_tenths)
    var congestion = Congestion(board)
    var stop = StopSignals()

    var begin = perf_counter_ns()
    # Route only these net codes, as if the others were absent.
    if settings.only:
        var codes = Set[Int]()
        for code in settings.only.split(","):
            codes.add(atol(code))
        var chosen = List[Int]()
        for i in order:
            if nets[i].id in codes:
                chosen.append(i)
        order = chosen^
    # A non-zero seed jitters every cell's starting price a little, so a retry finds different
    # routes even where the net order alone changes nothing.
    if seed != 0:
        var random = MersenneTwister(seed)
        for cell in range(board.cells()):
            congestion.history[cell] = random.uniform(0.05)
    if settings.preset:
        read_presets(settings.preset, nets)
    # Nets with kept copper go first: laying it costs nothing, and the new routing then sees it.
    var first = List[Int]()
    var then = List[Int]()
    for i in order:
        if nets[i].has_preset():
            first.append(i)
        else:
            then.append(i)
    order = first^
    order.extend(then^)
    congestion.kept_price = settings.kept_price
    # With kept copper on the board, new connections pay to cross it from the
    # first round.
    for ref net in nets:
        if net.has_preset():
            congestion.present = schedule.present_start
            break

    print("Starting auto-routing...", flush=True)
    for i in order:
        if stop.requested():
            break
        search.route(nets[i], board, congestion)
    var rounds, in_conflict = _negotiate(
        nets,
        order,
        board,
        congestion,
        search,
        schedule,
        settings,
        stop,
        begin,
        1,
        settings.max_rounds,
    )
    # With the board settled at cheap vias, vias get dearer and the nets that
    # have them are routed again, pass after pass while that helps.
    if settings.via_pass > 0 and in_conflict == 0:
        search.set_via_factor(board, settings.via_pass)
        for _ in range(settings.via_passes):
            var seconds = Float64(perf_counter_ns() - begin) / 1e9
            if stop.requested() or (
                settings.seconds > 0 and seconds > settings.seconds / 2
            ):
                break
            var after = _fewer_vias(
                nets,
                order,
                board,
                congestion,
                search,
                schedule,
                settings,
                stop,
                begin,
                rounds,
            )
            if after == rounds:
                break
            rounds = after

    var total_ms = Float64(perf_counter_ns() - begin) / 1e6
    var vias, dead = 0, 0
    for ref net in nets:
        vias += net.via_count
        dead += Int(net.dead)
    print(
        String(args[1]),
        String(args[2]),
        board.cells(),
        len(nets),
        dead,
        Int(in_conflict == 0),
        rounds,
        in_conflict,
        vias,
        search.expansions,
        _fixed3(Float64(search.search_ns) / 1e6),
        _fixed3(total_ms),
        sep=",",
        flush=True,
    )
    # Stopped before every net was routed: say which have nothing.
    if stop.requested():
        for ref net in nets:
            if not net.connections and not net.kept and len(net.pads) > 1:
                net.dead = True
    # Tidying takes well under a second and only ever uses clear cells, so a
    # stopped run is tidied too.
    tidy(nets, board, congestion, search)
    if settings.kept_out:
        write_kept(settings.kept_out, nets)
    if len(args) > 9:
        write_dump(String(args[9]), nets, congestion)
