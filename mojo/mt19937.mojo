"""The 32-bit Mersenne Twister, as `std::mt19937`, so a seed jitters prices as the C++ router does."""

comptime _N = 624
comptime _M = 397


struct MersenneTwister(Movable):
    """`std::mt19937`."""

    var _state: Array[UInt32, _N]
    var _index: Int

    def __init__(out self, seed: UInt32):
        self._state = Array[UInt32, _N](fill=0)
        self._state[0] = seed
        for i in range(1, _N):
            var previous = self._state[i - 1]
            self._state[i] = UInt32(1812433253) * (
                previous ^ (previous >> 30)
            ) + UInt32(i)
        self._index = _N

    def next_u32(mut self) -> UInt32:
        """The next 32 random bits."""
        if self._index >= _N:
            for i in range(_N):
                var y = (self._state[i] & 0x80000000) | (
                    self._state[(i + 1) % _N] & 0x7FFFFFFF
                )
                var value = self._state[(i + _M) % _N] ^ (y >> 1)
                if y & 1 != 0:
                    value ^= 0x9908B0DF
                self._state[i] = value
            self._index = 0
        var y = self._state[self._index]
        self._index += 1
        y ^= y >> 11
        y ^= (y << 7) & 0x9D2C5680
        y ^= (y << 15) & 0xEFC60000
        y ^= y >> 18
        return y

    def uniform(mut self, high: Float32) -> Float32:
        """libstdc++'s `uniform_real_distribution<float>(0, high)`: one draw, scaled to [0, 1).
        """
        var unit = Float32(self.next_u32()) / Float32(4294967296.0)
        if unit >= 1.0:
            unit = Float32(0.99999994)  # nextafter(1.0f, 0.0f)
        return unit * high + Float32(0.0)
