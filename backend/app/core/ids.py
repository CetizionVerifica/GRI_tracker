"""UUIDv7 generation (RFC 9562) for time-ordered primary keys.

Python 3.12 and PostgreSQL 16 have no built-in UUIDv7, so ids are generated in the application.
"""

import secrets
import threading
import time
from collections.abc import Callable
from uuid import UUID

_MAX_COUNTER = 0xFFF  # rand_a is 12 bits


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class Uuid7Generator:
    """Generates UUIDv7 values that are strictly increasing within this process.

    rand_a is used as a counter within the same millisecond (RFC 9562 section 6.2, method 1).
    It starts at a random 11-bit value, which leaves headroom before it overflows. If the clock
    goes backwards, the last timestamp is reused, so ordering is never broken.
    """

    def __init__(self, clock_ms: Callable[[], int] = _now_ms) -> None:
        self._clock_ms = clock_ms
        self._lock = threading.Lock()
        self._last_ms = -1
        self._counter = 0

    def __call__(self) -> UUID:
        with self._lock:
            ms = self._clock_ms()
            if ms > self._last_ms:
                counter = secrets.randbits(11)
            else:
                ms = self._last_ms
                counter = self._counter + 1
                if counter > _MAX_COUNTER:
                    ms += 1
                    counter = secrets.randbits(11)
            self._last_ms, self._counter = ms, counter

        value = (
            (ms & 0xFFFF_FFFF_FFFF) << 80
            | 0x7 << 76  # version
            | counter << 64
            | 0b10 << 62  # RFC 9562 variant
            | secrets.randbits(62)
        )
        return UUID(int=value)


uuid7 = Uuid7Generator()
