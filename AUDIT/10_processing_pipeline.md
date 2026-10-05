# 10 — Module review: `processing/` (deterministic clinical pipeline)

Read in full: normalize.py L1–163, numbers.py L1–117, negation.py L1–58,
fst.py L1–173, terminology.py L1–139, confidence.py L1–67, bidi.py L1–101.
Data cross-check: data/corrections.yaml (835 rules; 3 `dangerous: true`,
3 `confidence: low` — the same 3 rules).

## Pipeline shape (verified against app.py L213–240)

`normalize()` → `find_low_confidence_medical_words()` gate →
`TerminologyEngine.apply()` (numeric + negation protection inside) →
`to_injected()` → paste. Processing objects are per-session
(`TerminologyEngine` built in `LiveMedicalSTT.__init__`, app.py:147–150);
no module-level mutable state in any of the 7 files (static rule tables
are immutable `frozenset`/tuples).

## Findings

**F1 · Info · Rule `confidence` is parsed but never enforced.** FACT:
terminology.py:71 stores `confidence`; `_is_active` (L106–113) filters only
on `dangerous` and category. Currently harmless — verified: all 3
`confidence: low` rules in data/corrections.yaml are also
`category: unsafe` + `dangerous: true`, so they are blocked via another path.
But a future low-confidence *safe* rule would be applied silently; the field
is an unenforced promise in the schema.

**F2 · Info · Comment/table drift on ':' in normalize.** FACT:
normalize.py:76–78 says '/', '.', ':', '-' are "deliberately excluded" from
punctuation spacing, but L81 includes ':' in `_PUNCT_NO_SPACE_BEFORE`.
Behaviorally safe: `_looks_numeric_context` (L84–90) guards any
digit-adjacent ':', so "14:30" and "120/80" survive; the comment, not the
code, is what's wrong.

**F3 · Info · `_NUM` accepts ',' as a decimal separator** (numbers.py:17
`\d+(?:[.,]\d+)?`), so English thousands ("1,000") are protected as one
span — conservative over-protection, never under-protection. Correct bias
for this pipeline.

## Clinical-safety guarantees (verified, not assumed)

- Numeric protection: `find_numeric_spans` (numbers.py:61–83) covers BP
  ratios (`120/80`), ranges (`5-10 mg`, `2 تا 4 cm`), number+unit with
  optional route (`500 mg IV`), `%`, dates (incl. Jalali `1403/06/12`),
  times, and bare numbers as a last resort (L23–55, ordered
  most-specific-first with an occupancy mask, L65). The FST drops any match
  overlapping a protected span (fst.py:118–120, `_overlaps_any` L160–166).
- Negation protection: fixed marker list (negation.py:25–37), longest-match
  alternation (L39–40), spans protected the same way
  (terminology.py:121–130). Text of the markers themselves can't be partially
  rewritten.
- Unsafe rules: 3/835 rules are `dangerous`/`unsafe` and are excluded
  (terminology.py:106–108); unknown categories demote to
  `context_dependent`, which is opt-in and defaults off (L43–46, L29–31) —
  fail-closed.
- Low-confidence ASR words: `medical_risk_category` flags numbers, units,
  abbreviations, drug-suffix words (incl. Persian پنی‌سیلین with optional
  ZWNJ, confidence.py:12–16), procedures/diagnoses (L27–32); when any falls
  below threshold, the app preserves the provider text and skips terminology
  rewriting entirely (app.py:222–226) — the "don't upgrade a guess into an
  authoritative term" rule is enforced at the call site.
- FST determinism: `iter_long` longest-match + explicit re-resolution
  (fst.py:123–147), conflicts resolved to lexicographically smallest target
  and reported (L72–84); empty rule set is a pass-through (L86–89).

## Persian text handling (verified, feeding module 18)

- Arabic→Persian letterforms folded explicitly (ي→ی, ك→ک, ة→ه, …,
  normalize.py:28–36) — NFKC alone does not do this (comment L22–27 is
  accurate).
- Persian ۰-۹ *and* Arabic-Indic ٠-٩ digits → ASCII (L44–52); Arabic
  decimal/thousands/percent separators mapped (L54–64).
- ZWNJ: duplicates collapsed, both-sides-spaced artifacts → space, edge
  artifacts removed; mid-word ZWNJ (`می\u200cخواهم`) never deleted
  (L68–74, 136–141); ZWJ removed outright (L147).
- BiDi: logical/display/injected kept distinct; `to_injected` adds at most
  one leading RLM/LRM by first-strong rule (bidi.py:49–60, 95–110) and
  deliberately avoids RLE/PDF embedding (docstring L10–19 — forcing
  embedding would corrupt mixed "فشار خون 120/80 mmHg" in UBA-correct
  targets); Tk overlay gets visual-order text via python-bidi with a safe
  fallback (L82–91).

## Performance notes (measured in hardening session, re-confirmed by design)

YAML is parsed once (cached in config) and the FST is rebuilt per session —
the automaton build for 835 rules is sub-10 ms (50 sessions measured at
0.77 s total including everything, session-start to shutdown). No per-
transcript I/O anywhere in these 7 modules; all tables are process-lifetime
constants.

## Summary (3 lines)

The deterministic pipeline implements exactly the promised safety layers:
numeric/negation spans are untouchable, unsafe rules fail closed, ASR
low-confidence gates rewriting, and Persian letterform/digit/ZWNJ/BiDi
handling is explicit and correct. No defects found in the safety logic; the
only notes are an unenforced `confidence` field and one stale comment.
