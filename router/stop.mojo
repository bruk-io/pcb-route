"""Stopping on SIGTERM or SIGINT: finish the net in hand, then write the result as it stands."""

from std.ffi import c_int, external_call

comptime SIGINT: c_int = 2
comptime SIGTERM: c_int = 15
comptime _SIG_BLOCK: c_int = 0
comptime _SIGSET_BYTES = 128
"""The size of glibc's `sigset_t`."""


def _to_main_thread(signal: c_int) abi("C"):
    """Hands a stop signal that landed on one of the Mojo runtime's own threads to the main
    thread, which blocks it: there it stays pending until the router next asks.
    """
    var pid = external_call["getpid", c_int]()
    _ = external_call["tgkill", c_int](pid, pid, signal)


struct StopSignals(Movable):
    """Whether SIGTERM or SIGINT has arrived.

    The main thread blocks both and the router asks between nets. The Mojo runtime runs
    threads of its own, which would otherwise take the default action and end the process
    at once, so a signal delivered to one of them is passed on to the main thread.
    """

    var _arrived: Bool

    def __init__(out self):
        self._arrived = False
        var stops = Array[UInt8, _SIGSET_BYTES](fill=0)
        _ = external_call["sigemptyset", c_int](Pointer(to=stops))
        _ = external_call["sigaddset", c_int](Pointer(to=stops), SIGTERM)
        _ = external_call["sigaddset", c_int](Pointer(to=stops), SIGINT)
        _ = external_call["sigprocmask", c_int](
            _SIG_BLOCK,
            Pointer(to=stops),
            OptionalPointer[UInt8, MutUntrackedOrigin](),
        )
        comptime Handler = OpaquePointer[MutUntrackedOrigin]
        _ = external_call["signal", Handler](SIGTERM, _to_main_thread)
        _ = external_call["signal", Handler](SIGINT, _to_main_thread)

    def requested(mut self) -> Bool:
        """Whether a stop signal has arrived."""
        if not self._arrived:
            var pending = Array[UInt8, _SIGSET_BYTES](fill=0)
            _ = external_call["sigpending", c_int](Pointer(to=pending))
            self._arrived = (
                external_call["sigismember", c_int](
                    Pointer(to=pending), SIGTERM
                )
                == 1
                or external_call["sigismember", c_int](
                    Pointer(to=pending), SIGINT
                )
                == 1
            )
        return self._arrived
