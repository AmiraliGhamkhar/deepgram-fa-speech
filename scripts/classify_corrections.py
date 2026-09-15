#!/usr/bin/env python3
"""One-off migration script: classify the flat corrections.yaml rules into
the categorized/metadata schema used by medical_stt.processing.terminology.

This script is NOT part of the runtime application. It was used to produce
data/corrections.yaml from the legacy flat "from/to" list, and is kept for
transparency/reproducibility. Re-running it against a from/to-only file will
regenerate the categorized structure (manual review is still required).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "corrections.yaml"

# Persian phonetic spellings of the English alphabet (as dictated letter by
# letter). Rules whose source is entirely made of these tokens are spelled
# acronyms ("آی سی یو" = I-C-U) and are low risk: the physician was reading
# out an abbreviation, not using an ordinary word.
LETTER_TOKENS = {
    "آی", "ای", "بی", "سی", "دی", "اف", "جی", "اچ", "کی", "کا", "ال", "ام",
    "ان", "او", "پی", "کیو", "آر", "اس", "تی", "یو", "وی", "دبلیو", "ایکس",
    "وای", "زد", "زی", "سیو",  # "سیو" occurs as a fused "C-U" tail
    "اند",  # "&" spelled as "and" (D&C dictated "دی اند سی")
}

# Explicit deny-list called out by the safety review: generic, high-frequency
# Persian words/phrases whose "abbreviation" target can invert or distort
# clinical meaning when the source appears in ordinary (non-abbreviated)
# speech. These are always dangerous and excluded from default rule sets.
DANGEROUS_SOURCES = {
    "ناشتا",  # "fasting" -- forcing NPO assumes a specific order context
    "کاهش",   # "decrease" -- forcing DC ("discontinue") can invert meaning
    "کم شدن",  # "decreasing" -- same risk as above
}

# Common single-word anatomy / vital-sign nouns. Mapping these to an English
# word or cryptic abbreviation is a *translation*, not a normalization, and
# is easy to over-trigger on ordinary descriptive speech ("دست او متورم است").
# They are kept as opt-in (requires_context) rather than removed outright,
# since many are genuinely useful once a specialty context is known.
GENERIC_SINGLE_WORD_TARGETS = {
    "نبض", "دما", "تنفس", "دست", "پا", "بینی", "پوست", "زانو", "هیپ", "آرنج",
    "فمور", "تیبیا", "تالوس", "جنین", "تخمدان", "استخوان", "مفصل", "بورس",
    "ریه", "قلب", "کبد", "صفرا", "کولون", "مقعد", "ورید", "آئورت", "گچ",
    "پین", "پیچ", "پلاک", "نایلون", "مش", "درن", "زخم", "اسکار", "خال",
    "توده", "کیست", "پولیپ", "تومور", "سنگ", "پرون", "واروس", "شلی",
    "قندخون", "قند خون", "فشارخون", "فشار خون", "وریدی",
}


def _tokens(source: str) -> List[str]:
    return [t for t in re.split(r"[\s\u200c\-]+", source) if t]


def _fully_segments_into_letters(bare: str) -> bool:
    """True if `bare` (no spaces/ZWNJ) can be split end-to-end into known
    Persian letter-name syllables, e.g. 'سیسیو' -> 'سی'+'سی'+'یو'."""
    n = len(bare)
    if n == 0:
        return False
    ok = [False] * (n + 1)
    ok[0] = True
    for i in range(1, n + 1):
        for tok in LETTER_TOKENS:
            j = i - len(tok)
            if j >= 0 and ok[j] and bare[j:i] == tok:
                ok[i] = True
                break
    return ok[n]


def looks_like_spelled_acronym(source: str, target: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9&/\-]{1,8}", target):
        return False
    toks = _tokens(source)
    if toks and all(t in LETTER_TOKENS for t in toks):
        return True
    bare = source.replace("\u200c", "").replace(" ", "").replace("-", "")
    return _fully_segments_into_letters(bare)


def classify(src: str, tgt: str) -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "from": src,
        "to": tgt,
        "category": "specialty_terminology",
        "confidence": "high",
        "requires_context": False,
        "dangerous": False,
    }

    if src in DANGEROUS_SOURCES:
        meta.update(category="unsafe", confidence="low", dangerous=True,
                     requires_context=True)
        return meta

    if looks_like_spelled_acronym(src, tgt):
        meta.update(category="abbreviation_expansion", confidence="high")
        return meta

    bare = src.replace("\u200c", "").replace(" ", "")
    if src in GENERIC_SINGLE_WORD_TARGETS or bare in {
        s.replace("\u200c", "").replace(" ", "") for s in GENERIC_SINGLE_WORD_TARGETS
    }:
        meta.update(category="context_dependent", confidence="medium",
                     requires_context=True)
        return meta

    # Single ordinary Persian word (no spaces) mapped to a short cryptic
    # target is the highest-risk shape for accidental corruption of casual
    # speech; treat conservatively even if not on the explicit deny-list.
    if " " not in src and "\u200c" not in src and re.fullmatch(r"[A-Za-z&/]{1,3}", tgt):
        meta.update(category="context_dependent", confidence="medium",
                     requires_context=True)
        return meta

    if len(_tokens(src)) >= 2 or len(src) >= 6:
        meta.update(category="specialty_terminology", confidence="high")
        return meta

    meta.update(category="safe_lexical", confidence="high")
    return meta


def _fix_literal_escapes(text: str) -> str:
    """The legacy file used single-quoted YAML scalars with '\\u200c'/'\\u200d'
    sequences. Single-quoted YAML does NOT interpret backslash escapes, so
    those rules never actually contained a ZWNJ/ZWJ character -- they
    contained the five literal characters backslash-u-2-0-0-c. Real Persian
    ZWNJ text could therefore never match those "variant" rules. Repair it
    here so the rule set matches real Unicode input, and so the migration is
    reproducible/auditable.
    """
    text = text.replace("\\u200c", "\u200c").replace("\\u200d", "\u200d")
    return text


def main() -> int:
    data = yaml.safe_load(SRC.read_text(encoding="utf-8")) or {}
    rules = data.get("rules") or []
    if rules and isinstance(rules[0], dict) and "category" in rules[0]:
        print("corrections.yaml already categorized; nothing to do.", file=sys.stderr)
        return 0

    seen = set()
    out = []
    for item in rules:
        src = item.get("from") or item.get("source")
        tgt = item.get("to") or item.get("target")
        if not src or tgt is None:
            continue
        src = _fix_literal_escapes(str(src))
        tgt = _fix_literal_escapes(str(tgt))
        key = (src, tgt)
        if key in seen:
            continue
        seen.add(key)
        out.append(classify(src, tgt))

    print(yaml.safe_dump({"rules": out}, allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
