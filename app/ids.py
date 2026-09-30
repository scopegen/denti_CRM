import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_counter = 0
_COUNTER_MAX = 0xFFF


def uuid7() -> uuid.UUID:
    """A UUID version 7: time ordered, so new rows land at the end of indexes.

    Layout: 48 bits of Unix time in milliseconds, the version (7), a 12 bit counter, the
    variant (binary 10), then 62 random bits. The counter goes up for ids made in the same
    millisecond (RFC 9562, method 1), so ids from this process always come out in the order
    they were made: two bill entries written together list in the order they were written.
    """
    global _last_ms, _counter
    with _lock:
        unix_ms = time.time_ns() // 1_000_000
        if unix_ms > _last_ms:
            _last_ms, _counter = unix_ms, 0
        elif _counter < _COUNTER_MAX:
            _counter += 1
        else:
            # More than 4096 ids in one millisecond: borrow the next millisecond.
            _last_ms, _counter = _last_ms + 1, 0
        unix_ms, counter = _last_ms, _counter
    rand = int.from_bytes(os.urandom(8), "big")
    value = (
        (unix_ms & ((1 << 48) - 1)) << 80
        | 0x7 << 76
        | counter << 64
        | 0b10 << 62
        | (rand & ((1 << 62) - 1))
    )
    return uuid.UUID(int=value)
