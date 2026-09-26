import uuid
from collections.abc import Iterator
from itertools import count

from app.core.ids import Uuid7Generator, uuid7


def _timestamp_ms(value: uuid.UUID) -> int:
    return value.int >> 80


def _counter(value: uuid.UUID) -> int:
    return (value.int >> 64) & 0xFFF


def _fixed_clock(*readings: int) -> Iterator[int]:
    yield from readings


def test_version_and_variant() -> None:
    value = uuid7()

    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_embeds_clock_milliseconds() -> None:
    generate = Uuid7Generator(clock_ms=lambda: 1_790_000_000_123)

    assert _timestamp_ms(generate()) == 1_790_000_000_123


def test_strictly_increasing_and_unique() -> None:
    values = [uuid7() for _ in range(10_000)]

    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_same_millisecond_increments_counter() -> None:
    generate = Uuid7Generator(clock_ms=lambda: 1_000)

    first, second = generate(), generate()

    assert _timestamp_ms(first) == _timestamp_ms(second) == 1_000
    assert _counter(second) == _counter(first) + 1
    assert first < second


def test_counter_overflow_moves_to_next_millisecond() -> None:
    generate = Uuid7Generator(clock_ms=lambda: 1_000)

    values = [generate() for _ in range(5_000)]  # more than a 12-bit counter holds

    assert values == sorted(values)
    assert _timestamp_ms(values[-1]) > 1_000


def test_clock_going_backwards_keeps_order() -> None:
    readings = _fixed_clock(5_000, 4_000, 4_500)
    generate = Uuid7Generator(clock_ms=lambda: next(readings))

    values = [generate(), generate(), generate()]

    assert values == sorted(values)
    assert {_timestamp_ms(v) for v in values} == {5_000}


def test_timestamps_follow_the_clock() -> None:
    ticks = count(1_000, 7)
    generate = Uuid7Generator(clock_ms=lambda: next(ticks))

    assert [_timestamp_ms(generate()) for _ in range(3)] == [1_000, 1_007, 1_014]
