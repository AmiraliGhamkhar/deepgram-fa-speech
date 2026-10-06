"""Medical-safety terminology engine tests (task sections 3 & 9)."""
from __future__ import annotations

from medical_stt.processing.terminology import TerminologyEngine, parse_rules


def _rule(source, target, **kw):
    d = {"from": source, "to": target}
    d.update(kw)
    return d


def test_dangerous_rule_never_applied_by_default():
    engine = TerminologyEngine()
    engine.load([_rule("ناشتا", "NPO", category="unsafe", dangerous=True)])
    assert engine.apply("بیمار ناشتا است") == "بیمار ناشتا است"


def test_context_dependent_rule_disabled_by_default():
    engine = TerminologyEngine(enable_context_dependent=False)
    engine.load([_rule("نبض", "HR", category="context_dependent", requires_context=True)])
    assert engine.apply("نبض بیمار طبیعی است") == "نبض بیمار طبیعی است"


def test_context_dependent_rule_can_be_opted_in():
    engine = TerminologyEngine(enable_context_dependent=True)
    engine.load([_rule("نبض", "HR", category="context_dependent", requires_context=True)])
    assert "HR" in engine.apply("نبض بیمار طبیعی است")


def test_safe_lexical_and_abbreviation_rules_apply_by_default():
    engine = TerminologyEngine()
    engine.load([
        _rule("آی سی یو", "ICU", category="abbreviation_expansion"),
        _rule("سکته قلبی", "MI", category="specialty_terminology"),
    ])
    result = engine.apply("بیمار در آی سی یو بستری شد و سابقه سکته قلبی دارد")
    assert "ICU" in result and "MI" in result


def test_numeric_expression_protected_from_terminology_rewrite():
    engine = TerminologyEngine()
    # A deliberately adversarial rule whose source happens to appear inside
    # a numeric expression; it must not fire there.
    engine.load([_rule("120", "ONE_TWENTY", category="specialty_terminology")])
    assert engine.apply("BP 120/80 mmHg") == "BP 120/80 mmHg"


def test_negation_marker_protected_from_terminology_rewrite():
    engine = TerminologyEngine()
    engine.load([_rule("ندارد", "POSITIVE_FLIP", category="specialty_terminology")])
    assert engine.apply("علائم عفونت ندارد") == "علائم عفونت ندارد"


def test_legacy_flat_rules_default_to_specialty_terminology():
    rules = parse_rules([{"from": "آی سی یو", "to": "ICU"}])
    assert rules[0].category == "specialty_terminology"
    assert not rules[0].dangerous


def test_unknown_category_treated_as_context_dependent():
    rules = parse_rules([{"from": "x", "to": "Y", "category": "totally_unknown"}])
    assert rules[0].category == "context_dependent"
    assert rules[0].requires_context


def test_rule_count_excludes_dangerous_and_context_dependent_by_default():
    engine = TerminologyEngine(enable_context_dependent=False)
    engine.load([
        _rule("a", "A", category="safe_lexical"),
        _rule("b", "B", category="unsafe", dangerous=True),
        _rule("c", "C", category="context_dependent", requires_context=True),
    ])
    assert engine.rule_count == 1
    assert engine.total_rule_count == 3


def test_empty_terminology_is_noop():
    engine = TerminologyEngine()
    engine.load([])
    assert engine.apply("سلام دنیا") == "سلام دنیا"


def test_apply_on_empty_string():
    engine = TerminologyEngine()
    engine.load([_rule("a", "A")])
    assert engine.apply("") == ""


def test_actual_corrections_yaml_loads_and_blocks_dangerous_rules():
    """Regression guard: the real data/corrections.yaml must classify the
    known-dangerous generic terms as unsafe/blocked by default."""
    from medical_stt.config import load_correction_rules_raw

    raw = load_correction_rules_raw()
    engine = TerminologyEngine(enable_context_dependent=False)
    engine.load(raw)

    assert engine.apply("بیمار ناشتا است") == "بیمار ناشتا است"
    assert engine.apply("کاهش وزن داشته است") == "کاهش وزن داشته است"


def test_reload_replaces_the_rule_set_instead_of_unioning_it():
    """DeterministicFST.load() is documented as single-use.

    pyahocorasick accepts add_word() after make_automaton(), so reusing the
    automaton silently kept the first load's rules live while rule_count and
    total_rule_count described only the second. apply() then rewrote text with
    rules the engine reported it did not have -- unacceptable for a rewriter
    whose whole selling point is that its rule set is auditable.
    """
    engine = TerminologyEngine()
    engine.load([_rule("آی سی یو", "ICU", category="abbreviation_expansion")])
    assert engine.apply("آی سی یو") == "ICU"

    engine.load([_rule("بیوپسی", "Biopsy", category="abbreviation_expansion")])
    assert engine.rule_count == 1
    assert engine.total_rule_count == 1
    assert engine._fst.rule_count == 1, "the automaton kept the previous load's rules"
    assert engine.apply("آی سی یو") == "آی سی یو", "a dropped rule still fired"
    assert engine.apply("بیوپسی") == "Biopsy"


def test_reload_can_narrow_the_rule_set_to_nothing():
    engine = TerminologyEngine()
    engine.load([_rule("آی سی یو", "ICU", category="abbreviation_expansion")])
    engine.load([])
    assert engine.rule_count == 0
    assert engine.apply("آی سی یو") == "آی سی یو"


def test_reload_after_a_dangerous_rule_is_still_skipped():
    engine = TerminologyEngine()
    engine.load([_rule("آی سی یو", "ICU", category="abbreviation_expansion")])
    engine.load(
        [
            _rule("بیوپسی", "Biopsy", category="abbreviation_expansion"),
            _rule("منفی", "مثبت", category="abbreviation_expansion", dangerous=True),
        ]
    )
    assert engine.rule_count == 1
    assert engine.total_rule_count == 2
    assert engine.apply("منفی") == "منفی", "a dangerous rule fired after a reload"
