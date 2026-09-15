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
