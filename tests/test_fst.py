"""Deterministic longest-match FST correctness tests (task section 2 & 9)."""
from __future__ import annotations

from medical_stt.processing.fst import DeterministicFST, Rule, load_rules_from_pairs


def test_longest_match_wins_over_shorter_prefix():
    fst = load_rules_from_pairs([("آی", "X"), ("آی سی یو", "ICU")])
    assert fst.apply("آی سی یو") == "ICU"


def test_longest_match_classic_aho_corasick_example():
    # Classic example from the pyahocorasick docs: {"he","her","here"} on
    # "he here her" must yield "he", "here", "her" (never a short match
    # swallowing part of a longer one).
    fst = load_rules_from_pairs([("he", "HE"), ("her", "HER"), ("here", "HERE")])
    assert fst.apply("he here her") == "HE HERE HER"


def test_overlapping_rules_prefer_longest_at_each_position():
    fst = load_rules_from_pairs([("ab", "SHORT"), ("abc", "LONG")])
    assert fst.apply("xabcy") == "xLONGy"
    assert fst.apply("xaby") == "xSHORTy"


def test_prefix_rule_and_suffix_rule_do_not_interfere():
    fst = load_rules_from_pairs([("pre", "PRE-"), ("fix", "-FIX")])
    assert fst.apply("prefix") == "PRE--FIX"


def test_adjacent_rules_both_apply():
    fst = load_rules_from_pairs([("foo", "F"), ("bar", "B")])
    assert fst.apply("foobar") == "FB"
    assert fst.apply("foo bar") == "F B"


def test_repeated_rule_applies_each_occurrence():
    fst = load_rules_from_pairs([("aa", "A")])
    assert fst.apply("aaaa") == "AA"
    fst2 = load_rules_from_pairs([("ab", "X")])
    assert fst2.apply("abab") == "XX"


def test_persian_zwnj_variants():
    fst = load_rules_from_pairs([("سی\u200cسی\u200cیو", "CCU")])
    assert fst.apply("سی\u200cسی\u200cیو") == "CCU"
    # No accidental match without the ZWNJ present.
    fst_no_match = load_rules_from_pairs([("سی\u200cسی\u200cیو", "CCU")])
    assert fst_no_match.apply("سی سی یو") == "سی سی یو"


def test_mixed_persian_english_input():
    fst = load_rules_from_pairs([("سکته قلبی", "MI"), ("نوار قلب", "ECG")])
    text = "بیمار سابقه سکته قلبی دارد و نوار قلب گرفته شد"
    result = fst.apply(text)
    assert "MI" in result
    assert "ECG" in result
    assert "سکته قلبی" not in result


def test_no_match_input_is_unchanged():
    fst = load_rules_from_pairs([("آی سی یو", "ICU")])
    text = "این یک جمله بدون هیچ اصطلاح پزشکی است"
    assert fst.apply(text) == text


def test_empty_text_and_unready_fst():
    fst = DeterministicFST()
    assert fst.apply("") == ""
    assert fst.apply("hello") == "hello"  # not loaded yet -> passthrough


def test_deterministic_regardless_of_rule_insertion_order():
    pairs_a = [("آی سی یو", "ICU"), ("آی", "X"), ("سی یو", "Y")]
    pairs_b = [("سی یو", "Y"), ("آی", "X"), ("آی سی یو", "ICU")]
    fst_a = load_rules_from_pairs(pairs_a)
    fst_b = load_rules_from_pairs(pairs_b)
    text = "آی سی یو و آی و سی یو"
    assert fst_a.apply(text) == fst_b.apply(text)


def test_duplicate_source_conflict_resolved_deterministically():
    fst = DeterministicFST()
    result = fst.load([Rule("x", "B"), Rule("x", "A")])
    assert result.conflicts and result.conflicts[0][0] == "x"
    # Lexicographically smallest target chosen deterministically.
    assert fst.apply("x") == "A"


def test_empty_source_rules_are_skipped():
    fst = DeterministicFST()
    result = fst.load([Rule("", "SHOULD_NOT_APPEAR"), Rule("a", "A")])
    assert result.empty_sources_skipped == 1
    assert fst.apply("a") == "A"


def test_protected_ranges_prevent_rewriting():
    fst = load_rules_from_pairs([("120", "ONE_TWENTY")])
    text = "BP 120/80"
    # Protect the whole numeric expression.
    protected = [(3, 9)]
    assert fst.apply(text, protected_ranges=protected) == text


def test_rule_count_reflects_deduplicated_sources():
    fst = DeterministicFST()
    result = fst.load([Rule("a", "A"), Rule("a", "A"), Rule("b", "B")])
    assert result.rules_loaded == 2
    assert fst.rule_count == 2
