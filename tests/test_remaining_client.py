"""Full-prompt P1 gaps not covered by the earlier inferred follow-up batch."""
from __future__ import annotations

import inspect


import pytest

from medical_stt.app import LiveMedicalSTT
from medical_stt.config import Settings
from medical_stt.stt.accumulator import UtteranceAccumulator
from medical_stt.stt.base import TranscriptEvent, WordInfo
from medical_stt.stt.deepgram_provider import DeepgramProvider
from tests.test_asr_replacements import _load_pair


@pytest.mark.parametrize("unit", ["گرم", "میلی‌گرم", "میلیگرم", "میلی لیتر", "کیلوگرم", "درجه", "سانتی‌متر", "میلی‌مول"])
@pytest.mark.parametrize("target_side", [False, True])
def test_persian_asr_units_are_blocked(tmp_path, monkeypatch, unit, target_side):
    from medical_stt.config import ConfigError
    with pytest.raises(ConfigError):
        _load_pair(tmp_path, monkeypatch, *(('جایگزین', unit) if target_side else (unit, 'جایگزین')))


def test_unequal_word_counts_are_not_proof_of_duplicate_audio():
    accumulator = UtteranceAccumulator()
    first = TranscriptEvent('بله بله', True, speech_final=False, words=(WordInfo('بله بله', 0, 2, .9),))
    second = TranscriptEvent('بله بله', True, speech_final=True,
                             words=(WordInfo('بله', 0, 1, .9), WordInfo('بله', 1, 2, .9)))
    accumulator.add(first)
    result = accumulator.add(second)
    assert result.text == 'بله بله بله بله'


def test_connection_counter_reads_under_connection_lock():
    provider = DeepgramProvider(Settings())
    class Lock:
        def __enter__(self):
            provider._connections_opened = 7
        def __exit__(self, *_):
            pass
    provider._connection_lock = Lock()
    assert provider.connections_opened == 7


def test_interim_paths_share_one_normalization_helper():
    assert hasattr(LiveMedicalSTT, '_display_interim')
    source = inspect.getsource(LiveMedicalSTT)
    assert source.count('self._display_interim(event)') == 2


def test_final_latency_rotation_precedes_bounded_queue_post():
    source = inspect.getsource(LiveMedicalSTT._run_session)
    post = source.index('finals.put_nowait')
    rotation = source.index('receive_latency = LatencyTracker()', source.index('def handle_transcript'))
    assert rotation < post


def test_drop_warning_documents_deliberate_stickiness():
    assert 'sticky' in LiveMedicalSTT._report_audio_health.__doc__.lower()
