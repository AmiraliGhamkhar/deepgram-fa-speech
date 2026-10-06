"""Bounded audio queue drop/observability tests (task section 8)."""
from __future__ import annotations

from medical_stt.audio import BoundedAudioQueue


def test_put_and_get_roundtrip():
    q = BoundedAudioQueue(maxsize=4)
    assert q.put_nowait(b"abc") is True
    assert q.get(timeout=0.1) == b"abc"


def test_overflow_is_counted_and_never_raises():
    q = BoundedAudioQueue(maxsize=2)
    assert q.put_nowait(b"1") is True
    assert q.put_nowait(b"2") is True
    assert q.put_nowait(b"3") is False  # dropped, must not raise
    stats = q.stats()
    assert stats.dropped_total == 1


def test_stats_track_depth_and_totals():
    q = BoundedAudioQueue(maxsize=10)
    for i in range(5):
        q.put_nowait(str(i).encode())
    stats = q.stats()
    assert stats.put_total == 5
    assert stats.max_depth >= 1
    q.get()
    stats2 = q.stats()
    assert stats2.get_total == 1


def test_dropped_total_accumulates_across_overflows():
    q = BoundedAudioQueue(maxsize=1)
    q.put_nowait(b"a")
    q.put_nowait(b"b")  # dropped
    q.put_nowait(b"c")  # dropped
    assert q.stats().dropped_total == 2


def test_qsize_reflects_current_depth():
    q = BoundedAudioQueue(maxsize=5)
    q.put_nowait(b"a")
    q.put_nowait(b"b")
    assert q.qsize() == 2


def test_drain_discards_buffered_audio_on_stop():
    """Audio captured before Stop must not be streamed into the next session."""
    q = BoundedAudioQueue(maxsize=10)
    for i in range(5):
        q.put_nowait(str(i).encode())

    assert q.drain() == 5
    assert q.qsize() == 0
    assert q.stats().drained_total == 5


def test_drain_on_an_empty_queue_is_a_noop():
    q = BoundedAudioQueue(maxsize=4)
    assert q.drain() == 0
    assert q.stats().drained_total == 0


def test_drain_accumulates_across_sessions():
    q = BoundedAudioQueue(maxsize=10)
    q.put_nowait(b"a")
    q.put_nowait(b"b")
    q.drain()
    q.put_nowait(b"c")
    q.drain()
    assert q.stats().drained_total == 3


def test_empty_drain_resets_a_degraded_streak_after_sender_consumes_queue():
    q = BoundedAudioQueue(maxsize=1)
    assert q.put_nowait(b"queued") is True
    for _ in range(3):
        assert q.put_nowait(b"dropped") is False
    assert q.stats().degraded is True

    # The sender can consume the only queued chunk before the session reset.
    assert q.get(timeout=0.1) == b"queued"
    q.task_done()
    assert q.qsize() == 0

    assert q.drain() == 0
    stats = q.stats()
    assert stats.consecutive_drops == 0
    assert stats.degraded is False
    assert stats.dropped_total == 3  # cumulative diagnostics remain intact
