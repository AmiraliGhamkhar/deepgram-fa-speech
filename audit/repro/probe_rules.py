import sys, json, collections
sys.path.insert(0, ".")
from medical_stt.config import load_correction_rules_raw
from medical_stt.processing.terminology import TerminologyEngine, parse_rules

raw = load_correction_rules_raw()
rules = parse_rules(raw)
eng = TerminologyEngine()
res = eng.load(raw)
print("total rules:", len(rules), "active:", eng.rule_count, "loaded:", res.rules_loaded)
act = [r for r in rules if r.source in {x.source for x in rules}]
# recompute active same way engine does
active = [r for r in rules if eng._is_active(r)]
by_cat = collections.Counter(r.category for r in active)
print("active by category:", dict(by_cat))
# shortest active sources
active_sorted = sorted(active, key=lambda r: (len(r.source), r.source))
print("\n--- 40 shortest ACTIVE (default) sources ---")
for r in active_sorted[:40]:
    print(f"len={len(r.source):2d} {r.source!r} -> {r.target!r}  [{r.category}/{r.confidence}]")
# single-token active sources with Latin targets
print("\n--- active single-token sources with Latin-only target (possible silent rewrites) ---")
n = 0
for r in active_sorted:
    tgt = r.target
    if len(r.source.split()) == 1 and all(ord(ch) < 128 for ch in tgt) and len(r.source) <= 5:
        print(f"len={len(r.source):2d} {r.source!r} -> {tgt!r}  [{r.category}/{r.confidence}]")
        n += 1
print("count:", n)
