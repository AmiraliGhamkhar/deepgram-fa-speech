"""Audio capture with a lightweight, instrumented bounded queue.

The microphone callback (see `BoundedAudioQueue.put_nowait`) must never
block or do meaningful work: sounddevice calls it on a real-time audio
thread, and any delay there causes audible dropouts.

What that costs, stated precisely because the queue is instrumented: the
statistics (dropped-chunk counting, depth tracking, degraded/recovered
streaks) are guarded by an explicit `threading.Lock` that the callback *does*
take, on both the enqueue and the drop path. That is a deliberate trade, not
an oversight -- the counters are read from the UI and the session-health
report, so a torn read would be worse than a few hundred nanoseconds of
uncontended lock. The lock is never held across a `queue.Queue` operation,
across an allocation that can block, or across anything the consumer owns, so
it cannot be contended by the sender thread for longer than one counter
update. The *audio* itself is only ever handed to `queue.Queue.put_nowait`,
which never blocks and never raises for a full queue.
"""
from .queue import BoundedAudioQueue, QueueStats

__all__ = ["BoundedAudioQueue", "QueueStats"]
