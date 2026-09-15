"""Deterministic, reversible-in-spirit Persian/English text normalization.

Design goals (see README "Terminology philosophy"):

* Deterministic: same input always yields the same output.
* Safe: never touches clinically meaningful numbers, decimals, percentages,
  units, doses, dates or ranges.
* Unicode-correct: uses NFKC normalization plus a small, explicit table for
  Arabic-vs-Persian letterforms that NFKC does not unify (ي/ی, ك/ک, ...).
* ZWNJ-aware: normalizes ZWNJ *usage* (collapses accidental doubles/spacing
  around it) without ever deleting a ZWNJ that legitimately joins a compound
  word (removing it would silently change the word).

This module intentionally does NOT expand abbreviations or rewrite medical
terms -- that is the terminology engine's job (processing/terminology.py).
"""
from __future__ import annotations

import re
import unicodedata

ZWNJ = "\u200c"
ZWJ = "\u200d"

# Arabic letterforms commonly produced by ASR/typing that should be
# canonicalized to their standard Persian counterparts. NFKC does not fold
# these (they are distinct, both "valid" letters), so we do it explicitly.
_ARABIC_TO_PERSIAN = str.maketrans(
    {
        "ي": "ی",  # Arabic yeh -> Persian yeh
        "ك": "ک",  # Arabic kaf -> Persian keheh
        "ة": "ه",  # teh marbuta -> heh (approximation, common in casual text)
        "ؤ": "و",
        "إ": "ا",
        "أ": "ا",
        "ٱ": "ا",
        "ۀ": "ه",
    }
)

# Persian and Arabic-Indic digits -> ASCII digits. Converting the *digit
# glyphs* is safe and required for numeric-expression validation downstream;
# it does not change the numeric value or add/remove separators.
_PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_ARABIC_INDIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_DIGIT_TRANSLATION = str.maketrans(
    {**{d: str(i) for i, d in enumerate(_PERSIAN_DIGITS)},
     **{d: str(i) for i, d in enumerate(_ARABIC_INDIC_DIGITS)}}
)

# Arabic decimal separator (٫) and thousands separator (٬) -> ASCII.
_ARABIC_DECIMAL_SEP = "\u066b"
_ARABIC_THOUSANDS_SEP = "\u066c"
_ARABIC_PERCENT = "\u066a"

_SEPARATOR_TRANSLATION = str.maketrans(
    {
        _ARABIC_DECIMAL_SEP: ".",
        _ARABIC_THOUSANDS_SEP: ",",
        _ARABIC_PERCENT: "%",
    }
)

# Collapse runs of plain whitespace (never ZWNJ) to a single space.
_WHITESPACE_RUN_RE = re.compile(r"[ \t\r\f\v]{2,}")
_NEWLINE_RUN_RE = re.compile(r"\n{3,}")
_LEADING_TRAILING_RE = re.compile(r"^[ \t\r\f\v\u200c]+|[ \t\r\f\v\u200c]+$")

# A ZWNJ with whitespace on BOTH sides cannot be joining a compound word
# (real mid-word ZWNJ never has surrounding spaces) -- it is an ASR/typing
# artifact between two otherwise separate words, so it collapses to a single
# ordinary space rather than fusing the words together.
_ZWNJ_BOTH_SIDES_SPACED_RE = re.compile(r"\s+" + ZWNJ + r"\s+")
_ZWNJ_DUPLICATED_RE = re.compile(ZWNJ + "{2,}")

# Persian/Arabic punctuation that should have no space before it and one
# space after it, mirroring standard typographic conventions. We deliberately
# exclude '/', '.', ':', '-' from this table because those are load-bearing
# in numeric expressions (e.g. "120/80", "7.2", "10:00", ranges) and must be
# left completely alone here; numeric protection happens in numbers.py.
_PUNCT_NO_SPACE_BEFORE = "،؛؟!,;:?!"
_PUNCT_SPACE_AFTER = "،؛؟!,;?!"


def _looks_numeric_context(text: str, index: int) -> bool:
    """True if the character at `index` sits inside/adjacent to a digit
    run, so punctuation-spacing rules must not touch it."""
    left = text[index - 1] if index > 0 else ""
    right = text[index + 1] if index + 1 < len(text) else ""
    return left.isdigit() or right.isdigit()


def _fix_punctuation_spacing(text: str) -> str:
    out: list[str] = []
    n = len(text)
    for i, ch in enumerate(text):
        if ch in _PUNCT_NO_SPACE_BEFORE and not _looks_numeric_context(text, i):
            while out and out[-1] == " ":
                out.pop()
        out.append(ch)
        if (
            ch in _PUNCT_SPACE_AFTER
            and i + 1 < n
            and text[i + 1] not in " \n"
            and not _looks_numeric_context(text, i)
        ):
            out.append(" ")
    return "".join(out)


def normalize(text: str) -> str:
    """Deterministically normalize `text` while preserving clinical numeric
    fidelity and legitimate ZWNJ word-joins.

    This function is intentionally conservative: normalization runs before
    terminology rewriting, and the terminology engine independently protects
    numeric expressions (see processing/numbers.py), so both layers must
    agree on what counts as "do not touch".
    """
    if not text:
        return text

    # 1. Unicode canonicalization (compatibility decomposition + recompose).
    #    Safe for ZWNJ/ZWJ: NFKC does not remove or reorder them.
    text = unicodedata.normalize("NFKC", text)

    # 2. Arabic -> Persian letterform canonicalization.
    text = text.translate(_ARABIC_TO_PERSIAN)

    # 3. Digit and numeric-separator canonicalization (glyphs only; values
    #    and separator semantics are unchanged, just written in ASCII).
    text = text.translate(_DIGIT_TRANSLATION)
    text = text.translate(_SEPARATOR_TRANSLATION)

    # 4. ZWNJ cleanup: collapse accidental duplicates and a ZWNJ that has
    #    whitespace on both sides (never a real mid-word join), but never
    #    delete a ZWNJ that still joins two letters (e.g. "می\u200cخواهم").
    text = _ZWNJ_DUPLICATED_RE.sub(ZWNJ, text)
    text = _ZWNJ_BOTH_SIDES_SPACED_RE.sub(" ", text)
    # A ZWNJ at the very edge of the string, or immediately touching
    # whitespace on exactly one side with nothing to join on the other,
    # cannot be joining a compound word; it is a stray artifact.
    text = re.sub(ZWNJ + r"(?=\s|$)", "", text)
    text = re.sub(r"(?<=\s)" + ZWNJ, "", text)
    text = re.sub(r"^" + ZWNJ, "", text)
    # Remove the invisible Zero-Width Joiner outright: it never carries
    # meaning in standard Persian orthography and is an artifact of some
    # ASR engines.
    text = text.replace(ZWJ, "")

    # 5. Whitespace normalization (spaces only; never touches ZWNJ, and
    #    intentionally does not join separate lines).
    text = _WHITESPACE_RUN_RE.sub(" ", text)
    text = _NEWLINE_RUN_RE.sub("\n\n", text)

    # 6. Punctuation spacing, skipping anything that looks numeric.
    text = _fix_punctuation_spacing(text)

    # 7. Trim.
    text = _LEADING_TRAILING_RE.sub("", text)

    return text
