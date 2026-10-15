"""Repro: does benchmarks/metrics.py score a perfect hypothesis as perfect?

Report-only probe. No network, no credentials, no opt-in env vars.
"""
import sys
import unicodedata
import re
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import yaml  # noqa: E402  (after sys.path setup, like benchmarks/run_benchmark.py)
from benchmarks import metrics as M  # noqa: E402  (after sys.path setup, like benchmarks/run_benchmark.py)
from benchmarks.run_benchmark import apply_processing  # noqa: E402  (after sys.path setup, like benchmarks/run_benchmark.py)
from medical_stt.processing.numbers import find_numeric_spans  # noqa: E402  (after sys.path setup, like benchmarks/run_benchmark.py)

corpus = yaml.safe_load((ROOT / "benchmarks/corpus.yaml").read_text(encoding="utf-8"))
cases = corpus["cases"]
print(f"corpus cases: {len(cases)}")

print("\n[1] hypothesis == reference (an exact match must be a perfect score)")
bad = []
for case in cases:
    ref = str(case.get("reference", ""))
    s = M.score_case(case, ref)
    checks = {"wer": (s.wer, 0.0), "cer": (s.cer, 0.0), "num": (s.numeric_accuracy, 1.0),
              "neg": (s.negation_preservation, 1.0), "eng": (s.english_term_recall, 1.0),
              "med": (s.medical_term_recall, 1.0)}
    for name, (got, want) in checks.items():
        if got != want:
            bad.append((case.get("id"), name, got, want))
print(f"    imperfect metrics on an exact match: {len(bad)}")
for row in bad:
    print("     ", row)

print("\n[2] hypothesis = apply_processing(reference); negation markers identical?")
drops = [(c.get("id"), M.negation_preservation(str(c.get("reference")), apply_processing(str(c.get("reference")))))
         for c in cases]
drops = [d for d in drops if d[1] < 1.0]
print(f"    cases losing negation recall to processing: {len(drops)} {drops}")

print("\n[3] Latin tokens carrying punctuation in the corpus references")
punct = re.compile(r"[A-Za-z][A-Za-z0-9]*[^\w\sA-Za-z0-9]")
found = []
for case in cases:
    ref = str(case.get("reference", ""))
    for tok in M.tokens(ref):
        if punct.search(tok):
            found.append((case.get("id"), tok))
for case_id, tok in found:
    print(f"    {case_id}: token {tok!r} keeps its punctuation for recall scoring")
print(f"    -> {len(found)} token(s)")
for case in cases:
    ref = str(case.get("reference", ""))
    if not any(t for t in M.tokens(ref) if M._has_latin(t)):
        continue
    stripped = " ".join(M.tokens(ref))
    s = M.score_case(case, ref)
    s2 = M.score_case(case, stripped)
    if s2.english_term_recall < s.english_term_recall:
        print(f"    english_term_recall drops {s.english_term_recall:.2f} -> {s2.english_term_recall:.2f} "
              f"for {case.get('id')} when the hypothesis has no punctuation")

print("\n[4] presentation marks and normalization forms in the hypothesis")
base = "بیمار تب ندارد و SpO2 98 درصد است"
for label, hyp in (
    ("identical", base),
    ("LRM inside a Latin run", base.replace("SpO2", "\u200eSpO2")),
    ("NFD", unicodedata.normalize("NFD", base)),
    ("Persian digits", base.replace("98", "۹۸")),
):
    print(f"    {label:<24} neg={M.negation_preservation(base, hyp):.2f} "
          f"num={M.numeric_accuracy(base, hyp):.2f} wer={M.word_error_rate(base, hyp):.3f} "
          f"cer={M.character_error_rate(base, hyp):.3f}")

print("\n[5] truncation guard (documented: '120/8' must not satisfy '120/80')")
print("    ", M.numeric_accuracy("فشار 120/80", "فشار 120/8"),
      M.numeric_accuracy("فشار 120/80", "فشار 1120/80"),
      M.numeric_accuracy("فشار 120/80", "فشار 120/80"))

print("\n[6] a bare number credited through a Latin run?")
for hyp in ("D5W", "G5", "5"):
    print(f"    ref='دوز دکستروز 5 درصد' hyp={hyp!r} -> num={M.numeric_accuracy('دوز دکستروز 5 درصد', hyp):.2f}"
          f"  spans={[s.text for s in find_numeric_spans('دوز دکستروز 5 درصد')]}")
