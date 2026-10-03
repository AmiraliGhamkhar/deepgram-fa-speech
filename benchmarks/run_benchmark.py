#!/usr/bin/env python3
"""Reproducible STT benchmark harness (offline scoring + env-gated live run).

Offline mode (default; no network, no credentials, what CI can run):

    python benchmarks/run_benchmark.py --corpus benchmarks/corpus.yaml \
        --hypotheses my_run.json [--apply-processing] [--out report.json]

`my_run.json` maps case ids to the transcript a system produced for
`benchmarks/corpus.yaml`. Score a *raw* ASR run and again with
`--apply-processing` to see what the local deterministic layer adds.

Live mode (`--live`) streams `--audio-dir/<case-id>.wav` (16 kHz, mono,
16-bit PCM) through the real streaming provider, using the same
host-minted short-lived session as the desktop app:

    MEDICAL_STT_BENCHMARK_LIVE=1 \
    MEDICALSTT_HOST_URL=https://host.example.com \
    MEDICALSTT_HOST_SECRET=... \
    python benchmarks/run_benchmark.py --live --audio-dir recordings/

Live mode needs network access, a configured host and real recordings, so
it is never part of the normal test suite. Without them it exits 3 with an
explanation and measures nothing.

This harness does not ship accuracy numbers: the repository contains no
measured baseline, and none is implied. Run it against your own audio.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import wave
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402  (after sys.path setup)

from benchmarks.metrics import CorpusScore, score_corpus  # noqa: E402

DEFAULT_CORPUS = Path(__file__).resolve().parent / "corpus.yaml"


def load_corpus(path: Path) -> List[Dict[str, object]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = data.get("cases") or []
    if not isinstance(cases, list) or not cases:
        raise SystemExit(f"error: {path} has no 'cases' list")
    return cases


def load_hypotheses(path: Path) -> Dict[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("error: hypothesis file must be a JSON object of id -> text")
    out: Dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            # tolerate {"case": {"transcript": "..."}} style exports
            value = value.get("transcript") or value.get("text") or value.get("hypothesis") or ""
        out[str(key)] = str(value)
    return out


def apply_processing(text: str) -> str:
    """Run the local deterministic pipeline (normalize -> terminology -> injected)."""
    from medical_stt.config import load_correction_rules_raw
    from medical_stt.injection.text_injector import normalize_injected_whitespace
    from medical_stt.processing.bidi import to_injected
    from medical_stt.processing.normalize import normalize
    from medical_stt.processing.terminology import TerminologyEngine

    engine = TerminologyEngine()
    engine.load(load_correction_rules_raw())
    return normalize_injected_whitespace(to_injected(engine.apply(normalize(text))))


def print_report(score: CorpusScore, missing: Sequence[str]) -> None:
    print(f"{'case':<28} {'category':<18} {'WER':>6} {'CER':>6} {'num':>5} {'neg':>5} {'eng':>5}")
    print("-" * 80)
    for case in score.cases:
        print(
            f"{case.case_id:<28} {case.category:<18} {case.wer:>6.3f} {case.cer:>6.3f} "
            f"{case.numeric_accuracy:>5.2f} {case.negation_preservation:>5.2f} "
            f"{case.english_term_recall:>5.2f}"
        )
    print("-" * 80)
    for key, value in score.aggregate().items():
        print(f"{key:<28} {value}")
    print("\nBy category:")
    for category, values in score.by_category().items():
        print(
            f"  {category:<20} n={int(values['cases'])} wer={values['mean_wer']:.3f} "
            f"cer={values['mean_cer']:.3f} num={values['mean_numeric_accuracy']:.2f}"
        )
    if missing:
        print(f"\nNo hypothesis supplied for {len(missing)} case(s): {', '.join(missing)}")
        print("(They are excluded from the means above; that is not the same as scoring 0.)")


# -- live mode -------------------------------------------------------------


def _read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise SystemExit(f"error: {path} is not 16-bit PCM")
        return handle.readframes(handle.getnframes())


def run_live(cases: Sequence[Dict[str, object]], audio_dir: Path) -> Dict[str, str]:
    """Stream each recording through the real provider and collect finals."""
    if os.environ.get("MEDICAL_STT_BENCHMARK_LIVE") != "1":
        print(
            "Live benchmarking is disabled. It makes real Deepgram connections and\n"
            "costs money, so it is opt-in: set MEDICAL_STT_BENCHMARK_LIVE=1 and\n"
            "MEDICALSTT_HOST_URL / MEDICALSTT_HOST_SECRET to enable it.",
            file=sys.stderr,
        )
        raise SystemExit(3)

    from medical_stt.config import get_settings, load_keyterms
    from medical_stt.stt.base import ProviderError
    from medical_stt.stt.deepgram_provider import DeepgramProvider

    settings = get_settings()
    if not settings.host_url or not settings.host_secret:
        print("error: host_url / host_secret are not configured", file=sys.stderr)
        raise SystemExit(3)

    provider = DeepgramProvider(settings, keyterms=load_keyterms(settings.specialty))
    provider.validate_config()

    hypotheses: Dict[str, str] = {}
    for case in cases:
        case_id = str(case.get("id"))
        wav = audio_dir / f"{case_id}.wav"
        if not wav.is_file():
            print(f"skip {case_id}: {wav} not found")
            continue
        audio = _read_wav(wav)
        finals: List[str] = []
        errors: List[ProviderError] = []

        def on_transcript(event, collected: List[str] = finals) -> None:  # noqa: ANN001
            if event.is_final and event.text:
                collected.append(event.text)

        def on_error(error: ProviderError, collected: List[ProviderError] = errors) -> None:
            collected.append(error)

        provider.start(on_transcript, on_error)
        chunk_bytes = settings.sample_rate * 2 // 25  # ~40 ms
        try:
            for start in range(0, len(audio), chunk_bytes):
                provider.send_audio(audio[start:start + chunk_bytes])
        finally:
            provider.stop()
        if errors:
            print(f"{case_id}: provider error {errors[0].category.value}: {errors[0]}")
        hypotheses[case_id] = " ".join(finals).strip()
    return hypotheses


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--hypotheses", help="JSON file mapping case id -> transcript")
    parser.add_argument("--apply-processing", action="store_true",
                        help="score the local post-processing output instead of the raw text")
    parser.add_argument("--out", help="write the JSON report here")
    parser.add_argument(
        "--spoken-reference", action="store_true",
        help="with --apply-processing: score against the spoken reference instead of the "
             "corpus-declared expected written form (shows the terminology rewrites as edits)",
    )
    parser.add_argument("--live", action="store_true", help="stream --audio-dir recordings (env-gated)")
    parser.add_argument("--audio-dir", help="directory of <case-id>.wav recordings for --live")
    args = parser.parse_args(argv)

    cases = load_corpus(Path(args.corpus))

    if args.live:
        if not args.audio_dir:
            print("error: --live requires --audio-dir", file=sys.stderr)
            return 2
        hypotheses = run_live(cases, Path(args.audio_dir))
    elif args.hypotheses:
        hypotheses = load_hypotheses(Path(args.hypotheses))
    else:
        print(
            "Nothing to score: pass --hypotheses <file.json> (offline) or --live --audio-dir <dir>.\n"
            "This repository intentionally ships no measured accuracy numbers; see\n"
            "README > Benchmarking for how to produce them from your own recordings.",
            file=sys.stderr,
        )
        return 2

    if args.apply_processing:
        hypotheses = {case_id: apply_processing(text) for case_id, text in hypotheses.items()}

    expect_written = bool(args.apply_processing) and not args.spoken_reference
    if expect_written:
        print(
            "Scoring against the corpus's expected written forms: terminology rewrites "
            "declared in benchmarks/corpus.yaml are not counted as errors.\n"
            "(Use --spoken-reference to see them as edits.)\n"
        )
    score, missing = score_corpus(cases, hypotheses, expect_written_terms=expect_written)
    print_report(score, missing)

    if args.out:
        report = {
            "corpus": str(args.corpus),
            "hypotheses": args.hypotheses or "live",
            "apply_processing": bool(args.apply_processing),
            "reference_used": "expected_written" if bool(args.apply_processing) and not args.spoken_reference else "spoken",
            "aggregate": score.aggregate(),
            "by_category": score.by_category(),
            "cases": [case.as_dict() for case in score.cases],
            "missing": list(missing),
        }
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nReport written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
