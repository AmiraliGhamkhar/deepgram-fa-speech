import sys
sys.path.insert(0, ".")
from medical_stt.processing.normalize import normalize
from medical_stt.processing.numbers import find_numeric_spans
from medical_stt.config import load_correction_rules_raw, load_asr_replacements
from medical_stt.processing.terminology import TerminologyEngine

CASES = [
    "فشار خون 120/80 mmHg است",
    "دوز ۵۰۰ میلی‌گرم هر ۸ ساعت",
    "HbA1c برابر 7.2 درصد است",
    "تب ۳۸٫۵ درجه دارد",
    "دمای 38.5 C",
    "قند خون ناشتا 126 mg/dL بود",
    "۱۰ تا ۲۰ میلی‌لیتر",
    "وزن 72/5 کیلوگرم",           # ratio-like with . and /
    "در تاریخ 1403/06/12 بستری شد",
    "ساعت 10:30 ویزیت شد",
    "درد 8/10 گزارش شد",
    "فشار خون 120/80 میلی\u200cجیوه است",
    "مشکل تنفسی دارد",
    "امید به زندگی",
    "بیمار بدون تب بود",
    "ناشتا است",
    "1000 mg",
    "1,000 mg",
    "۲۵ درصد",
    "سطح هموگلوبین 12.4 g/dl",
    "کاهش وزن 5 کیلوگرم",
    "پلاکت 450000",
    "شمارش گلبول سفید 8000/mm3",
    "دوز 0.5 mg/kg/day",
]
eng = TerminologyEngine()
eng.load(load_correction_rules_raw())
print(f"{'INPUT':45s} | NORMALIZED | TERMINOLOGY")
for c in CASES:
    n = normalize(c)
    t = eng.apply(n)
    idem = "IDEM-OK" if normalize(n) == n else "NOT-IDEMPOTENT"
    spans = [(s.start, s.end, s.text) for s in find_numeric_spans(n)]
    print(f"{c!r}")
    print(f"   norm : {n!r}  [{idem}]")
    print(f"   term : {t!r}")
    print(f"   spans: {spans}")
