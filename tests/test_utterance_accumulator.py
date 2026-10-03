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


def test_repeated_legitimate_utterance_is_kept_without_metadata() -> None:
    """"بله" twice are two utterances, not one duplicated event.

    With no word timings there is no proof of a repeat, so identical text
    must be preserved (the conservative direction).
    """
    accumulator = UtteranceAccumulator()
    accumulator.add(TranscriptEvent("بله", is_final=True, speech_final=False))
    accumulator.add(TranscriptEvent("بله", is_final=True, speech_final=False))
    utterance = accumulator.add(TranscriptEvent("", is_final=True, speech_final=True))
    assert utterance is not None
    assert utterance.text == "بله بله"


def test_repeated_phrase_with_distinct_timings_is_kept() -> None:
    accumulator = UtteranceAccumulator()
    first = WordInfo("بله", 1.0, 1.4, 0.9)
    second = WordInfo("بله", 3.0, 3.4, 0.9)
    accumulator.add(TranscriptEvent("بله", True, False, 0.9, (first,)))
    accumulator.add(TranscriptEvent("بله", True, False, 0.9, (second,)))
    utterance = accumulator.add(TranscriptEvent("", True, True))
    assert utterance is not None
    assert utterance.text == "بله بله"
    assert utterance.words == (first, second)


def test_same_segment_resent_with_identical_timings_is_deduplicated() -> None:
    accumulator = UtteranceAccumulator()
    words = (WordInfo("دوز", 1.0, 1.3, 0.9), WordInfo("5", 1.3, 1.5, 0.8))
    accumulator.add(TranscriptEvent("دوز 5", True, False, 0.9, words))
    accumulator.add(TranscriptEvent("دوز 5", True, False, 0.9, words))
    utterance = accumulator.add(TranscriptEvent("", True, True))
    assert utterance is not None
    assert utterance.text == "دوز 5"
    assert utterance.words == words


def test_same_event_object_twice_is_deduplicated() -> None:
    """Without metadata, the identical event object is provable duplication."""
    accumulator = UtteranceAccumulator()
    event = TranscriptEvent("دوز 5 mg", is_final=True, speech_final=False)
    accumulator.add(event)
    accumulator.add(event)
    utterance = accumulator.add(TranscriptEvent("", is_final=True, speech_final=True))
    assert utterance is not None
    assert utterance.text == "دوز 5 mg"


def test_repeated_text_after_a_flush_is_kept() -> None:
    accumulator = UtteranceAccumulator()
    first = accumulator.add(TranscriptEvent("بله", True, True))
    second = accumulator.add(TranscriptEvent("بله", True, True))
    assert first is not None and second is not None
    assert first.text == "بله" and second.text == "بله"


def test_word_metadata_is_combined() -> None:
    accumulator = UtteranceAccumulator()
    first = WordInfo("دوز", 0.0, 0.2, 0.9)
    second = WordInfo("5", 0.3, 0.4, 0.6)
    accumulator.add(TranscriptEvent("دوز", True, False, 0.9, (first,)))
    utterance = accumulator.add(TranscriptEvent("5", True, True, 0.6, (second,)))
    assert utterance is not None
    assert utterance.words == (first, second)
    assert utterance.confidence == 0.75
