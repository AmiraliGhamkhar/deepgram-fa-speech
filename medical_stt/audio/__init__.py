"""Audio capture with a lightweight, instrumented bounded queue.

The microphone callback (see `BoundedAudioQueue.put_nowait`) must never
block or do meaningful work: sounddevice calls it on a real-time audio
thread, and any delay there causes audible dropouts. All accounting
(dropped-chunk counting, depth tracking) is done with plain counters, no
locks beyond what `queue.Queue` already provides internally.
"""
from .queue import BoundedAudioQueue, QueueStats

__all__ = ["BoundedAudioQueue", "QueueStats"]
