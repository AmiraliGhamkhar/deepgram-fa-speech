"""Benchmark harness: metric math and honest failure modes.

The metric tests use hand-computed values, so a regression in the harness
cannot silently inflate the numbers. The harness itself must never
"measure" anything without audio or hypotheses: an accidental default
would be indistinguishable from fabricated accuracy.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks.metrics import (
    character_error_rate,
    english_term_recall,
    medical_term_recall,
    negation_preservation,
    numeric_accuracy,
    score_corpus,
    word_error_rate,
)
from benchmarks.run_benchmark import apply_processing, load_corpus, main

ROOT = Path(__file__).resolve().parent.parent
BENCHMARK = ROOT / "benchmarks" / "run_benchmark.py"


# -- hand-computed metric values -------------------------------------------


def test_wer_is_zero_for_identical_text():
    assert word_error_rate("الف ب پ", "الف ب پ") == 0.0


def test_wer_counts_substitution_deletion_insertion():
    assert word_error_rate("a b c", "a x c") == pytest.approx(1 / 3)   # substitution
    assert word_error_rate("a b c", "a c") == pytest.approx(1 / 3)     # deletion
    assert word_error_rate("a b c", "a b c d") == pytest.approx(1 / 3)  # insertion
    # A reversal cannot be fixed by 2 edits: a monotonic alignment keeps at
    # most one token, so all four positions change.
    assert word_error_rate("a b c d", "d c b a") == pytest.approx(4 / 4)
    assert word_error_rate("a b c d", "a x c y") == pytest.approx(2 / 4)


def test_wer_edge_cases_are_defined():
    assert word_error_rate("", "") == 0.0
    assert word_error_rate("", "anything") == 1.0
    assert word_error_rate("a b", "") == 1.0


def test_cer_is_computed_over_characters():
    assert character_error_rate("abcd", "abcd") == 0.0
    assert character_error_rate("abcd", "abxd") == pytest.approx(0.25)


def test_numeric_accuracy_requires_the_exact_clinical_expression():
    reference = "فشار خون 120/80 mmHg و HbA1c 7.2 درصد"
    assert numeric_accuracy(reference, reference) == 1.0
    assert numeric_accuracy(reference, "فشار خون 120/80 و HbA1c 7.2 درصد") == pytest.approx(0.5)
    assert numeric_accuracy("بیمار بی‌حال بود", "بیمار بی‌حال بود") == 1.0


def test_numeric_accuracy_compares_persian_and_arabic_indic_digits_by_value():
    assert numeric_accuracy("فشار خون ۱۲۵/۸۰", "BP 125/80.") == 1.0
    assert numeric_accuracy("قند ١٢٦", "قند 126") == 1.0
    assert numeric_accuracy("قند 126", "قند ١٢٦") == 1.0
    assert numeric_accuracy("قند 126", "قند ١٢٧") == 0.0


def test_negation_preservation_detects_a_flip():
    assert negation_preservation("علائم عفونت ندارد", "علائم عفونت ندارد") == 1.0
    assert negation_preservation("علائم عفونت ندارد", "علائم عفونت دارد") == 0.0


def test_term_recall_counts_only_terms_present_in_the_reference():
    # Terms absent from the reference are not required (recall, not precision)...
    assert medical_term_recall("بیمار بستری شد", "بیمار بستری شد", ["ICU"]) == 1.0
    # ...but a term the reference does contain must survive the hypothesis.
    reference = "بیمار در ICU با SpO2 96 درصد بستری شد"
    assert medical_term_recall(reference, reference, ["ICU", "SpO2"]) == 1.0
    assert medical_term_recall(reference, "بیمار در بخش بستری شد", ["ICU"]) == 0.0


def test_english_term_recall_tracks_latin_tokens():
    reference = "SpO2 88 درصد و BP 130/85"
    assert english_term_recall(reference, reference) == 1.0
    assert english_term_recall(reference, "اشباع اکسیژن 88 درصد") == 0.0


# -- corpus + harness ------------------------------------------------------


def test_shipped_corpus_covers_every_documented_category():
    cases = load_corpus(ROOT / "benchmarks" / "corpus.yaml")
    categories = {case["category"] for case in cases}
    assert categories == {
        "normal_dictation", "terminology", "medications", "numbers", "blood_pressure",
        "spo2", "dosage", "fast_noisy_speech", "mixed_language", "repeated_phrases",
        "long_dictation",
    }
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))


def test_scoring_identical_hypotheses_yields_perfect_scores():
    cases = load_corpus(ROOT / "benchmarks" / "corpus.yaml")
    hypotheses = {case["id"]: case["reference"] for case in cases}
    score, missing = score_corpus(cases, hypotheses)
    assert missing == []
    assert score.mean_wer == 0.0
    assert score.mean_cer == 0.0
    assert score.aggregate()["mean_numeric_accuracy"] == 1.0
    assert score.aggregate()["mean_negation_preservation"] == 1.0


def test_iterating_processing_never_degrades_a_clinical_number():
    cases = load_corpus(ROOT / "benchmarks" / "corpus.yaml")
    hypotheses = {case["id"]: case["reference"] for case in cases}
    processed = {case_id: apply_processing(text) for case_id, text in hypotheses.items()}
    score, _ = score_corpus(cases, processed)
    assert score.aggregate()["mean_numeric_accuracy"] == 1.0
    assert score.mean_wer < 1.0  # some Persian phrasing is mapped to Latin terms


def test_harness_measures_nothing_without_input(tmp_path, capsys):
    exit_code = main(["--corpus", str(ROOT / "benchmarks" / "corpus.yaml")])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Nothing to score" in captured.err


def test_harness_scores_a_hypothesis_file_and_writes_a_report(tmp_path, capsys):
    cases = load_corpus(ROOT / "benchmarks" / "corpus.yaml")
    hypotheses = {case["id"]: case["reference"] for case in cases}
    hypotheses_path = tmp_path / "hyp.json"
    hypotheses_path.write_text(json.dumps(hypotheses, ensure_ascii=False), encoding="utf-8")
    report_path = tmp_path / "report.json"

    exit_code = main([
        "--corpus", str(ROOT / "benchmarks" / "corpus.yaml"),
        "--hypotheses", str(hypotheses_path),
        "--out", str(report_path),
    ])
    assert exit_code == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["aggregate"]["mean_wer"] == 0.0
    assert report["missing"] == []
    assert "no hypothesis supplied" not in capsys.readouterr().out.lower()


def test_live_mode_is_refused_without_the_opt_in_environment(monkeypatch):
    monkeypatch.delenv("MEDICAL_STT_BENCHMARK_LIVE", raising=False)
    result = subprocess.run(
        [sys.executable, str(BENCHMARK), "--live", "--audio-dir", "/nonexistent"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=60,
    )
    assert result.returncode == 3
    assert "opt-in" in result.stderr.lower() or "disabled" in result.stderr.lower()


def test_module_docstring_states_that_no_numbers_are_shipped():
    text = BENCHMARK.read_text(encoding="utf-8")
    assert "does not ship accuracy numbers" in text


def test_declared_expected_rewrites_match_the_deterministic_layer():
    """The corpus declares every terminology rewrite it expects.

    Running the local pipeline over the corpus's own references must
    therefore reproduce the declared written form exactly. If a rule in
    data/corrections.yaml changes one of these sentences, this fails with
    the expectation mismatch -- review the rule, then update the corpus
    deliberately (do not silently widen the expectation).
    """
    cases = load_corpus(ROOT / "benchmarks" / "corpus.yaml")
    hypotheses = {case["id"]: apply_processing(str(case["reference"])) for case in cases}
    score, missing = score_corpus(cases, hypotheses, expect_written_terms=True)
    assert missing == []
    assert score.mean_wer == 0.0
    assert score.mean_cer == 0.0


# -- boundary-aware containment -------------------------------------------
#
# Every recall/accuracy metric used `needle in haystack`. That credits a
# truncated digit and an abbreviation swallowed by a longer Latin word as a
# perfect hit -- the exact false positives a clinical benchmark exists to
# catch, and the reason the numbers in a report could not be trusted.


@pytest.mark.parametrize(
    "reference,hypothesis",
    [
        ("120/8", "120/80 mmHg"),      # a dropped digit must not be found in a longer one
        ("7.2", "7.25"),               # HbA1c 7.2 versus 7.25 is a different clinical value
        ("14:30", "14:300"),
        ("2024-01-05", "2024-01-055"),
        ("98", "98.5"),
        ("۱۲", "۱۲۳"),
    ],
)
def test_numeric_accuracy_rejects_a_number_embedded_in_a_longer_one(reference, hypothesis):
    assert numeric_accuracy(reference, hypothesis) == 0.0


@pytest.mark.parametrize(
    "reference,hypothesis",
    [
        ("120/80 mmHg", "BP 120/80 mmHg."),          # sentence punctuation is not an extension
        ("5-10 mg", "دوز 5-10 mg, سپس"),             # neither is a comma
        ("7.2 درصد", "HbA1c 7.2 درصد"),
        ("7.2", "HbA1c 7.2."),                       # decimal value before a full stop
        ("7.2", "HbA1c 7.2, سپس ادامه داد"),          # decimal value before a comma
        ("120/80 mmHg", "فشار خون 120/80 mmHg و ضربان 72"),
    ],
)
def test_numeric_accuracy_still_accepts_a_whole_expression_in_context(reference, hypothesis):
    """The boundary rule must not manufacture false negatives."""
    assert numeric_accuracy(reference, hypothesis) == 1.0


def test_numeric_accuracy_finds_the_whole_expression_after_a_truncated_mention():
    """Every occurrence is tested, not just the first."""
    assert numeric_accuracy("120/80", "شنیدم 120/8 یعنی 120/80") == 1.0


def test_english_term_recall_does_not_credit_an_abbreviation_inside_a_word():
    # "IV" is a substring of "DRIVE"; the speaker's IV order was not heard.
    assert english_term_recall("give IV push", "give DRIVE fast") == pytest.approx(1 / 3)


def test_english_term_recall_does_not_credit_a_shorter_form_of_a_token():
    assert english_term_recall("SpO2 88 درصد", "SpO22 88 درصد") == 0.0


def test_english_term_recall_accepts_a_token_next_to_punctuation():
    assert english_term_recall("SpO2 88 درصد", "نتیجه: SpO2=88 درصد.") == 1.0


def test_medical_term_recall_does_not_credit_a_term_inside_a_longer_word():
    # "MI" (myocardial infarction) is a substring of "ADMINISTRATION".
    assert medical_term_recall(
        "درد قفسه سینه و MI", "درد قفسه سینه و ADMINISTRATION", ["MI"]
    ) == 0.0


def test_medical_term_recall_ignores_a_term_only_embedded_in_the_reference():
    """The denominator counts terms the speaker actually said.

    A term list entry that appears only inside a longer reference word was
    being treated as spoken, which both inflated the denominator and then
    credited the hypothesis for a word it never produced.
    """
    assert medical_term_recall("دارو در حال ADMINISTRATION است", "دارو در حال ADMINISTRATION است", ["MI"]) == 1.0


def test_medical_term_recall_persian_terms_are_unchanged():
    """The boundary rule is ASCII-only, so Persian matching is untouched."""
    reference = "بیمار در ICU با SpO2 96 درصد بستری شد"
    assert medical_term_recall(reference, reference, ["بستری", "ICU"]) == 1.0
    assert medical_term_recall(reference, "بیمار در بخش بستری شد", ["بستری", "ICU"]) == 0.5
