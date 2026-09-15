from medical_stt.stt.accumulator import UtteranceAccumulator
from medical_stt.stt.base import TranscriptEvent, WordInfo


def test_interim_is_display_only_state() -> None:
    accumulator = UtteranceAccumulator()
    assert accumulator.add(TranscriptEvent("بیمار", is_final=False, speech_final=False)) is None
    assert accumulator.flush() is None


def test_final_segment_waits_for_speech_final() -> None:
    accumulator = UtteranceAccumulator()
    assert accumulator.add(TranscriptEvent("فشار خون", is_final=True, speech_final=False)) is None
    utterance = accumulator.add(TranscriptEvent("120/80", is_final=True, speech_final=True))
    assert utterance is not None
    assert utterance.text == "فشار خون 120/80"


def test_utterance_end_flushes_accumulated_segments() -> None:
    accumulator = UtteranceAccumulator()
    accumulator.add(TranscriptEvent("بیمار پایدار است", is_final=True, speech_final=False))
    utterance = accumulator.add(TranscriptEvent("", is_final=True, speech_final=True))
    assert utterance is not None
    assert utterance.text == "بیمار پایدار است"


def test_repeated_final_segment_is_not_duplicated() -> None:
    accumulator = UtteranceAccumulator()
    segment = TranscriptEvent("دوز 5 mg", is_final=True, speech_final=False)
    accumulator.add(segment)
    accumulator.add(segment)
    utterance = accumulator.add(TranscriptEvent("", is_final=True, speech_final=True))
    assert utterance is not None
    assert utterance.text == "دوز 5 mg"


def test_word_metadata_is_combined() -> None:
    accumulator = UtteranceAccumulator()
    first = WordInfo("دوز", 0.0, 0.2, 0.9)
    second = WordInfo("5", 0.3, 0.4, 0.6)
    accumulator.add(TranscriptEvent("دوز", True, False, 0.9, (first,)))
    utterance = accumulator.add(TranscriptEvent("5", True, True, 0.6, (second,)))
    assert utterance is not None
    assert utterance.words == (first, second)
    assert utterance.confidence == 0.75
