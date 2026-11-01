"""Shipped data catalogs: keyterms, ASR replacements, terminology rules.

Everything here reads a file that ships with the build and is never written by
the application. Each loader validates *at load time* -- a malformed rule is a
`ConfigError`, not a surprise during a consultation -- and each returns plain
data, so the processing layer never touches YAML.

The ASR-replacement guard deserves its own note: a provider-side
``find:replace`` is applied by Deepgram verbatim and bypasses every local
review, so anything carrying a clinical number, unit, abbreviation or a
Latin/English target is refused here rather than trusted downstream.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .. import paths
from ..processing.normalize import normalize
from .errors import ConfigError
from .validation import _valid_specialty_name
from .yaml_io import _load_yaml

log = logging.getLogger("medical_stt.config")

#: Directory of the shipped data files. A module-level value (not a call to
#: `paths`) so tests and packagers can redirect it in one place.
DATA_DIR: Path = paths.bundled_data_dir()

_ASR_BLOCKED_TOKENS = frozenset({
    "گرم", "میلیگرم", "میلی‌گرم", "کیلوگرم", "میلیلیتر", "میلی‌لیتر",
    "سانتیمتر", "سانتی‌متر", "میلیمول", "میلی‌مول", "درجه", "درصد",
    "mg", "mcg", "µg", "g", "kg", "ml", "l", "cc", "iu", "meq", "mmol", "mmhg",
    "iv", "im", "po", "sc", "hr", "bid", "tid", "qid", "prn", "npo",
    # These extra Latin clinical terms occur in data/corrections.yaml.
    "icu", "ecg", "mi", "diabetes", "hypertension",
    # Census-derived: every standalone uppercase Latin token of length >= 2
    # that appears in a shipped terminology rule. A provider-side
    # replacement must never introduce one of these without local review.
    'abc', 'abg', 'ac', 'acl', 'acr', 'afi', 'am', 'apache',
    'ards', 'arf', 'atfl', 'av', 'bd', 'bi', 'bmi', 'bnp',
    'bp', 'bpd', 'bph', 'bps', 'bs', 'bun', 'cbc', 'cbd',
    'cbr', 'ccu', 'chf', 'cmc', 'copd', 'cpr', 'cr', 'crif',
    'crl', 'crp', 'cts', 'cva', 'cvp', 'cxr', 'dbp', 'dc',
    'dj', 'ds', 'dvt', 'dw', 'dzp', 'edc', 'edv', 'ef',
    'efw', 'ekg', 'ems', 'ent', 'ercp', 'esr', 'ett', 'fbs',
    'ffp', 'fhr', 'fl', 'ga', 'gcs', 'ger', 'gfr', 'hc',
    'hdl', 'hf', 'hl', 'hps', 'hpv', 'htn', 'hvd', 'id',
    'ii', 'inr', 'iugr', 'ivc', 'lap', 'ldl', 'map', 'mcl',
    'md', 'mn', 'ng', 'ns', 'nt', 'ob', 'or', 'orif',
    'pcnl', 'pcwp', 'pds', 'pe', 'pfna', 'pi', 'plt', 'pm',
    'psa', 'psv', 'pt', 'ptt', 'qh', 'qs', 'rads', 'rbc',
    'ri', 'ros', 'rpoc', 'rr', 'sa', 'sbp', 'sofa', 'sol',
    'tds', 'tg', 'tha', 'tka', 'tsh', 'tul', 'tur', 'turp',
    'wbc',
})
_ASR_BLOCKED_TOKENS = frozenset(token.casefold() for token in _ASR_BLOCKED_TOKENS)
_ASR_LATIN_TOKEN_RE = re.compile(r"(?<!\w)[A-Za-zµμ]+(?!\w)")


def _has_blocked_asr_token(text: str) -> bool:
    # Fold joiners/spaces for Persian compound-unit spellings, without
    # weakening the existing whole-token Latin guard.
    persian = text.replace("\u200c", "")
    return (
        any(match.group().casefold() in _ASR_BLOCKED_TOKENS for match in _ASR_LATIN_TOKEN_RE.finditer(text))
        or any(re.search(r"(?<!\w)" + re.escape(unit.replace("\u200c", "")).replace("میلی", r"میلی[ \t]*").replace("سانتی", r"سانتی[ \t]*") + r"(?!\w)", persian)
               for unit in _ASR_BLOCKED_TOKENS if not unit.isascii())
    )


def _contains_non_ascii_letter(text: str) -> bool:
    """True if `text` contains a Persian/Arabic (non-ASCII) letter.

    Persian and Arabic letters are non-ASCII; Latin/English letters and
    ASCII units are ASCII. Used to tell a real provider-side spelling fix
    from one that would smuggle a Latin/English term into the transcript.
    """
    return any(not ch.isascii() and ch.isalpha() for ch in text)

MAX_KEYTERMS = 100

#: Deepgram's keyterm limit is 500 *tokens* per request, not a term count,
#: and the tokenizer is not public. Budgeting one token per two characters
#: is conservative for Persian and keeps the request well under the limit
#: (a connection over it is refused at the handshake).
MAX_KEYTERM_CHARS = 1000


def _terms_from_file(path: Path) -> List[str]:
    data = _load_yaml(path)
    terms = data.get("keyterms") if isinstance(data, dict) else data
    if terms is None:
        return []
    if not isinstance(terms, list):
        raise ConfigError(f"{path} must contain a 'keyterms' list")

    return [str(term).strip() for term in terms if term is not None and str(term).strip()]


def load_keyterms(
    specialty: str = "general", max_count: int = MAX_KEYTERMS, max_chars: int = MAX_KEYTERM_CHARS
) -> List[str]:
    """Load general plus selected specialty terms, preserving priority order.

    Both the term count and the total character budget are enforced in
    priority order: general terms first, then the specialty's, and loading
    stops at whichever limit is reached first.
    """
    if not _valid_specialty_name(specialty):
        raise ConfigError("invalid keyterm specialty name")
    if max_count < 0:
        raise ConfigError("maximum keyterm count must be non-negative")
    if max_count == 0:
        return []
    keyterm_dir = DATA_DIR / "keyterms"
    if keyterm_dir.is_dir():
        paths_to_load = [keyterm_dir / "general.yaml"]
        if specialty != "general":
            selected = keyterm_dir / f"{specialty}.yaml"
            if not selected.is_file():
                raise ConfigError(f"unknown keyterm specialty: {specialty!r}")
            paths_to_load.append(selected)
    else:
        # Existing deployments with only the original file continue to work.
        paths_to_load = [DATA_DIR / "keyterms.yaml"]

    result: List[str] = []
    seen = set()
    total_chars = 0
    for path in paths_to_load:
        for term in _terms_from_file(path):
            if term in seen:
                continue
            if total_chars + len(term) > max_chars:
                log.warning(
                    "keyterms truncated at %d terms: the %d-character budget "
                    "(Deepgram's 500-token limit) is exhausted by %s",
                    len(result), max_chars, path.name,
                )
                return result
            seen.add(term)
            result.append(term)
            total_chars += len(term)
            if len(result) == max_count:
                return result
    return result


def load_asr_replacements() -> List[str]:
    """Load safe Deepgram ``find:replace`` parameters (``replace`` feature).

    Ownership: this is the *only* provider-side text rewrite the project
    uses. It exists for acoustically confusable Persian spellings that the
    provider itself must fix, and its output then flows through the local
    pipeline (normalization -> terminology) like any other transcript. It
    must never carry clinical numbers, units or a Latin/English term that
    would bypass the locally-reviewed terminology rules -- Deepgram's own
    docs note that ``replace`` is applied verbatim and the find term must
    be lowercase. Violations are configuration errors, not warnings.
    """
    data = _load_yaml(DATA_DIR / "asr_replacements.yaml")
    replacements = data.get("replacements", []) if isinstance(data, dict) else []
    if not isinstance(replacements, list):
        raise ConfigError("data/asr_replacements.yaml 'replacements' must be a list")

    result: List[str] = []
    seen: Dict[str, str] = {}
    for item in replacements:
        if not isinstance(item, dict) or not item.get("from") or item.get("to") is None:
            continue
        source, target = str(item["from"]).strip(), str(item["to"]).strip()
        if not source or not target:
            raise ConfigError("ASR replacements must have a non-empty from and to")
        # Deepgram advertises the find term as lowercase-only; an uppercase
        # source silently never fires, which would look like an ASR bug.
        if source != source.lower():
            raise ConfigError(
                f"ASR replacement source {source!r} must be lowercase (Deepgram requirement)"
            )
        # Keep provider-side replacement away from clinical numbers; those
        # remain protected locally during all terminology operations.
        if any(ch.isdigit() for ch in source + target) or ":" in source + target:
            raise ConfigError("ASR replacements cannot contain digits or ':'")
        # Provider-side replacements bypass local clinical review: check
        # units/abbreviations on BOTH sides, even for ASCII-only pairs.
        # Keep the stricter no-Latin-target rule for Persian spelling fixes;
        # benign ASCII placeholders still work if neither side is clinical.
        if (
            "%" in source or "%" in target
            or _has_blocked_asr_token(source) or _has_blocked_asr_token(target)
            or (_contains_non_ascii_letter(source) and any(ch.isascii() and ch.isalpha() for ch in target))
        ):
            raise ConfigError(
                f"ASR replacement {source!r} -> {target!r} contains a "
                "Latin/English term or unit; those belong in "
                "data/corrections.yaml where the category/context guards apply"
            )
        if source in seen and seen[source] != target:
            raise ConfigError(
                f"ASR replacement conflict: {source!r} maps to both {seen[source]!r} and {target!r}"
            )
        if source in seen:
            continue  # identical duplicate: harmless, keep one
        seen[source] = target
        result.append(f"{source}:{target}")
    return result


def load_correction_rules_raw() -> List[Dict[str, Any]]:
    """Return the raw categorized rule dicts (see processing/terminology.py
    for the schema). Kept separate from the legacy pair-tuple loader so
    callers can access category metadata."""
    data = _load_yaml(DATA_DIR / "corrections.yaml")
    if not isinstance(data, dict) or "rules" not in data:
        if data:
            raise ConfigError("data/corrections.yaml must contain a top-level 'rules' list")
        return []
    rules = data.get("rules")
    if rules is None:
        return []
    if not isinstance(rules, list):
        raise ConfigError("data/corrections.yaml 'rules' must be a list")
    raw_rules = [r for r in rules if isinstance(r, dict)]
    from ..processing.terminology import parse_rules

    parse_rules(raw_rules)  # fail at load time, including for legacy flat callers
    return raw_rules


def load_correction_rules() -> List[Tuple[str, str]]:
    """Backward-compatible flat (source, target) pairs, ignoring category
    metadata. Prefer TerminologyEngine for anything safety-sensitive."""
    pairs: List[Tuple[str, str]] = []
    for item in load_correction_rules_raw():
        src = item.get("from") or item.get("source") or item.get("src")
        tgt = item.get("to") or item.get("target") or item.get("tgt")
        if src and tgt is not None:
            # Legacy flat callers must see the same canonical form as the
            # terminology engine: a raw Arabic-yeh source would otherwise stay
            # unmatchable after normalize() has run on the transcript.
            pairs.append((normalize(str(src)), normalize(str(tgt))))
    return pairs
