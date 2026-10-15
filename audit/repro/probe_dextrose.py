import sys
sys.path.insert(0, ".")
from medical_stt.config import load_correction_rules_raw
from medical_stt.processing.normalize import normalize
from medical_stt.processing.terminology import TerminologyEngine, parse_rules
from medical_stt.processing.numbers import find_numeric_spans

raw = load_correction_rules_raw()
rules = parse_rules(raw)
eng = TerminologyEngine()
eng.load(raw)
active_sources = {r.source for r in rules if eng._is_active(r)}

CASES = [
    "تزریق دکستروز 5 درصد در آب",
    "تزریق دکستروز ۵ درصد در آب",
    "دکستروز 5 درصد در آب",
    "دکستروز 5% در آب",
    "دکستروز 5 درصد در سالین",
    "لیزر CO2 انجام شد",
]
for c in CASES:
    n = normalize(c)
    print(f"input      : {c!r}")
    print(f"normalized : {n!r}")
    print(f"numeric    : {[(s.start, s.end, s.text) for s in find_numeric_spans(n)]}")
    print(f"terminology: {eng.apply(n)!r}")
    for r in rules:
        if r.source and r.source in n and r.source in active_sources:
            print(f"   active rule matching: {r.source!r} -> {r.target!r} [{r.category}]")
    print()
