"""Zeroed per-cell storage whose pages the system maps only on first touch."""

from std.ffi import c_int, c_size_t, external_call
from std.os import abort
from std.sys import CompilationTarget, size_of

comptime _HUGE_PAGE = 2 << 20
"""A transparent huge page on x86-64 and arm64 Linux."""


struct ZeroedBuffer[T: TrivialRegisterPassable](Movable, Sized):
    """A fixed number of values, all zero to begin with: one per grid cell.

    The memory is mapped, not cleared: the system maps its pages only when
    they are first touched, so an incremental run, which searches a small part
    of the board, never pays to clear the rest. On Linux a buffer of 2 MB or
    more asks for huge pages, so a touched region costs one page fault per
    2 MB instead of one per 4 KB. The stdlib's `alloc` has neither, which is
    why this type exists.

    Parameters:
        T: The type of the values; all-zero bytes must be a valid one.
    """

    var _data: Pointer[Self.T, MutUntrackedOrigin]
    var _count: Int
    var _mapping: Int
    """The address the mapping starts at, when the buffer is mapped."""
    var _mapped: Int
    """Bytes mapped; 0 when the buffer came from `calloc`."""

    def __init__(out self, count: Int):
        self._count = count
        var bytes = count * size_of[Self.T]()
        comptime if CompilationTarget.is_linux():
            if bytes >= _HUGE_PAGE:
                # Map a huge page more than needed, and start on a huge page
                # boundary: only whole aligned huge pages can be huge.
                self._mapped = bytes + _HUGE_PAGE
                # Addresses cross the C boundary as integers: a null
                # `Pointer` cannot be spelled, and these are only numbers.
                var mapping = external_call["mmap", Int](
                    Int(0),
                    c_size_t(self._mapped),
                    c_int(3),  # PROT_READ | PROT_WRITE
                    c_int(0x22),  # MAP_PRIVATE | MAP_ANONYMOUS
                    c_int(-1),
                    Int(0),
                )
                if mapping == -1:  # MAP_FAILED
                    abort(
                        "out of memory for a grid of "
                        + String(count)
                        + " cells"
                    )
                self._mapping = mapping
                var start = (mapping + _HUGE_PAGE - 1) & ~(_HUGE_PAGE - 1)
                _ = external_call["madvise", c_int](
                    start,
                    c_size_t(bytes),
                    c_int(14),  # MADV_HUGEPAGE
                )
                self._data = Pointer[Self.T, MutUntrackedOrigin](
                    unsafe_from_address=start
                )
                return
        self._mapped = 0
        self._mapping = 0
        var data = external_call[
            "calloc", OptionalPointer[Self.T, MutUntrackedOrigin]
        ](c_size_t(max(count, 1)), c_size_t(size_of[Self.T]()))
        if not data:
            abort("out of memory for a grid of " + String(count) + " cells")
        self._data = data.value()

    def __deinit__(deinit self):
        if self._mapped:
            _ = external_call["munmap", c_int](
                self._mapping, c_size_t(self._mapped)
            )
        else:
            external_call["free", NoneType](self._data)

    @inline(.always)
    def __getitem__(self, index: Int) -> Self.T:
        return self._data[unsafe_offset=index]

    @inline(.always)
    def __setitem__(mut self, index: Int, value: Self.T):
        self._data[unsafe_offset=index] = value

    def __len__(self) -> Int:
        return self._count

    @inline(.always)
    def unsafe_ptr(self) -> Pointer[Self.T, MutUntrackedOrigin]:
        """The first value, for vector loads in the hot loops; valid while
        the buffer lives."""
        return self._data


def zeroed[
    T: TrivialRegisterPassable
](copies: Int, count: Int) -> List[ZeroedBuffer[T]]:
    """One zeroed buffer of `count` values per net class."""
    var buffers = List[ZeroedBuffer[T]](capacity=copies)
    for _ in range(copies):
        buffers.append(ZeroedBuffer[T](count))
    return buffers^
