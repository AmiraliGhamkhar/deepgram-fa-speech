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
active = [r for r in rules if eng._is_active(r)]

dead = []
for r in active:
    src = normalize(r.source)
    if not src:
        continue
    out = eng.apply(src)
    if r.target not in out and src == out:
        dead.append(r)

print(f"active rules: {len(active)}  unreachable-by-own-source: {len(dead)}")
for r in dead:
    spans = [(s.start, s.end, s.text) for s in find_numeric_spans(normalize(r.source))]
    print(f"  {r.source!r} -> {r.target!r} [{r.category}/{r.confidence}] numeric-spans-in-source={spans}")
