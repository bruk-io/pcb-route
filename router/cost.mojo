"""Integer step costs: a straight step is `SCALE` units, times the cell's price."""

from std.math import floor

comptime SCALE = 10
"""Cost units of one straight step."""
comptime CAP = 4096
"""The most a cell's price may multiply a step's cost."""


@inline(.always)
def llround(x: Float64) -> Int:
    """C's `llround` for `x >= 0`: halves round away from zero."""
    var whole = floor(x)
    return Int(whole) + (1 if x - whole >= 0.5 else 0)


@inline(.always)
def step_units(multiple: Float64, base: Int) -> Int:
    """A step's cost: `base` units times the cell's price, within
    `base` and `base * CAP`."""
    return min(max(llround(multiple * Float64(base)), base), base * CAP)
