import sys
sys.path.insert(0, ".")
from medical_stt.config import load_correction_rules_raw
from medical_stt.processing.terminology import parse_rules, TerminologyEngine

raw = load_correction_rules_raw()
rules = parse_rules(raw)
eng = TerminologyEngine()
eng.load(raw)
active = [r for r in rules if eng._is_active(r)]
single = [r for r in active if len(r.source.split()) == 1]
print("active:", len(active), "single-token active:", len(single))
for cat in ("safe_lexical", "abbreviation_expansion", "specialty_terminology"):
    items = sorted([r for r in single if r.category == cat], key=lambda r: (len(r.source), r.source))
    print(f"\n=== {cat}: {len(items)} single-token active ===")
    for r in items:
        print(f"{r.source} -> {r.target}")
