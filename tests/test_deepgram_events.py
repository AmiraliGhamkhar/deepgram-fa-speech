from types import SimpleNamespace

from medical_stt.stt.deepgram_provider import transcript_event_from_result


def _result(*, text="دوز 5 mg", is_final=True, speech_final=False, words=None, confidence=0.88):
    alternative = SimpleNamespace(transcript=text, confidence=confidence, words=words)
    return SimpleNamespace(
        channel=SimpleNamespace(alternatives=[alternative]),
        is_final=is_final,
        speech_final=speech_final,
    )


def test_final_word_confidence_and_timestamps_are_parsed() -> None:
    word = SimpleNamespace(word="دوز", punctuated_word="دوز", start=1.2, end=1.6, confidence=0.72)
    event = transcript_event_from_result(_result(words=[word], speech_final=True))
    assert event is not None
    assert event.speech_final is True
    assert event.confidence == 0.88
    assert event.words[0].text == "دوز"
    assert event.words[0].start == 1.2
    assert event.words[0].end == 1.6
    assert event.words[0].confidence == 0.72


def test_missing_words_and_confidence_do_not_crash() -> None:
    event = transcript_event_from_result(_result(words=None, confidence=None))
    assert event is not None
    assert event.words == ()
    assert event.confidence is None


def test_interim_does_not_capture_words() -> None:
    word = SimpleNamespace(word="MI", punctuated_word=None, start=0, end=1, confidence=0.5)
    event = transcript_event_from_result(_result(is_final=False, words=[word]))
    assert event is not None
    assert event.words == ()


def test_empty_speech_final_result_can_flush_accumulator() -> None:
    event = transcript_event_from_result(_result(text="", speech_final=True, words=[]))
    assert event is not None
    assert event.text == ""
