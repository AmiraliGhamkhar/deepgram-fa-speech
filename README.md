# Medical STT

Real-time Persian + English medical dictation for Windows.

**Microphone → Deepgram Nova-3 streaming → deterministic normalization →
medical terminology processing → RTL/BiDi formatting → Windows text
injection → floating overlay.**

This is a **deterministic, rule-based dictation aid**, not a clinical NLP
system and not a medical device. See [Limitations](#limitations) before
using it in any clinical workflow.

## Architecture

```
medical_stt/
├── app.py                 # wires the pipeline together; reconnect loop
├── config.py              # settings + YAML loading & validation
├── audio/
│   └── queue.py           # bounded, instrumented audio queue
├── stt/
│   ├── base.py            # STTProvider interface + ErrorCategory taxonomy
│   ├── deepgram_provider.py  # Deepgram implementation of STTProvider
│   └── reconnect.py        # exponential backoff + retry/no-retry policy
├── processing/
│   ├── normalize.py        # Unicode/digit/whitespace normalization
│   ├── numbers.py           # protects clinical numeric expressions
│   ├── negation.py          # protects negation/fidelity phrases
│   ├── fst.py               # deterministic longest-match rewriter
│   ├── terminology.py       # safety-categorized terminology engine
│   └── bidi.py               # logical / display / injected text views
├── injection/
│   ├── backend.py            # InjectionBackend interface + DryRunBackend
│   ├── _windows_backend.py    # native Win32 SendInput + clipboard
│   ├── _fallback_backend.py    # pyautogui/pyperclip (Linux/macOS)
│   └── text_injector.py        # delta backspacing + paste orchestration
└── ui/
    └── overlay.py               # optional Tkinter floating overlay
```

**Provider abstraction:** `STTProvider` (in `stt/base.py`) is the interface
the rest of the pipeline depends on. `DeepgramProvider` is the only
implementation today, but the processing/injection layers never import
Deepgram directly — a future local (Whisper/Qwen) or alternative cloud
provider can be added by implementing `STTProvider` without touching
`processing/` or `injection/`.

**Injection abstraction:** `InjectionBackend` (in `injection/backend.py`)
isolates every OS-specific call. `TextInjector`'s logic (streaming delta
backspacing, clipboard-restore policy, BiDi-aware prefixing) is tested via
`DryRunBackend` on any platform, without a real Windows GUI.

## Install

```bash
git clone <this-repo>
cd deepgram-v6
python -m venv .venv
source .venv/bin/activate      # Linux/macOS
# .venv\Scripts\activate       # Windows

pip install -r requirements.txt
cp .env.example .env           # then edit .env and set DEEPGRAM_API_KEY
```

Dependency versions are pinned to explicitly tested ranges (see
`requirements.txt`); `deepgram-sdk==7.9.0` is pinned exactly because the
`listen.v1.connect(...)` call shape is version-sensitive.

For development/testing:

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
ruff check medical_stt tests scripts
mypy medical_stt
```

## Run

```bash
python run.py
```

Place the cursor in any text field (EMR, Word, browser form, etc.) and
speak. Final utterances are normalized, safely rewritten, and pasted
automatically. Stop with `Ctrl+C`.

## Configuration

| File | Purpose |
|------|---------|
| `.env` | `DEEPGRAM_API_KEY` (required, never commit this file) |
| `config/settings.yaml` | model, language, audio, injection, reconnection, terminology safety toggle |
| `data/keyterms.yaml` | Deepgram keyterm boosting list (sent as separate values, per the current API) |
| `data/corrections.yaml` | categorized terminology rules (see below) |

All settings are validated at startup (`medical_stt/config.py`); invalid
sample rate, channel count, block duration, endpointing, inject mode,
reconnect settings, model, or language fail fast with a clear error instead
of surfacing as a confusing runtime failure.

### Inject modes

- `paste` (default) — clipboard + Ctrl+V. Best for Persian BiDi shaping in
  most apps, because the target application's own Unicode Bidi Algorithm
  handles the mixed text.
- `type` — Unicode key events, still routed through the clipboard on
  non-Windows platforms (PyAutoGUI cannot emit non-ASCII directly on
  X11/macOS).

### Reconnection

`reconnect_delay`, `reconnect_backoff_max`, `reconnect_jitter`, and
`max_reconnect_attempts` control exponential backoff with bounded jitter
for **retryable** failures (network, timeout, rate limit, server
disconnect, microphone). **Authentication and configuration errors are
never retried** — see `medical_stt/stt/reconnect.py` and
`tests/test_streaming.py`.

## Terminology philosophy

`data/corrections.yaml` is **not** a blind find-and-replace dictionary.
Every rule carries metadata:

```yaml
- from: 'آی سی یو'
  to: 'ICU'
  category: abbreviation_expansion   # safe_lexical | abbreviation_expansion |
                                      # specialty_terminology |
                                      # context_dependent | unsafe
  confidence: high                   # high | medium | low
  requires_context: false            # opt-in only if true
  dangerous: false                   # never applied if true
```

- **`safe_lexical` / `abbreviation_expansion` / `specialty_terminology`**
  rules are applied by default (spelling normalization, spelled-out
  abbreviations like "آی سی یو" = I-C-U, and unambiguous multi-word
  specialty phrases).
- **`context_dependent`** rules (e.g. a single common word like "نبض"
  mapped to "HR") are **disabled by default**. They can be enabled with
  `enable_context_dependent_terms: true` in `config/settings.yaml`, after
  reviewing the risk for your dictation population.
- **`unsafe` / `dangerous`** rules are never applied automatically. Two
  concrete examples removed from the default behavior during this review:
  `"ناشتا"` (fasting) → `"NPO"` and `"کاهش"` (decrease) → `"DC"`
  (discontinue) — both can silently invert or over-specify clinical intent
  when the source word appears in ordinary speech, not an explicit order.

**Principle:** if uncertain, the system preserves the original transcript
rather than inventing or forcing a medical abbreviation. This is a
deterministic, rule-based system — no LLM is used anywhere in the
transcription-correction path.

Numeric clinical expressions (`120/80 mmHg`, `98%`, `5 mg`, `500 mg IV`,
date/time-like patterns, ranges) are protected by
`medical_stt/processing/numbers.py` and can never be partially rewritten by
a terminology rule. Recognized negation/fidelity phrases (`ندارد`, `وجود
ندارد`, `مشاهده نشد`, `بدون`, `منفی است`, `رد می‌شود`) are protected by
`medical_stt/processing/negation.py` for the same reason — see
`tests/regression/test_medical_cases.py`.

### The longest-match guarantee

The terminology engine (`processing/fst.py`) uses `pyahocorasick`'s
`iter_long()` to guarantee a **longest valid match at each position,
left-to-right, independent of YAML rule order**. This is verified in
`tests/test_fst.py` against the classic Aho-Corasick textbook case
(`{"he","her","here"}` on `"he here her"`) and against this repository's
own overlap: with both `"آی" → X` and `"آی سی یو" → ICU` defined, the input
`"آی سی یو"` becomes `"ICU"`, never a partial `"X سی یو"`.

## Mixed RTL/LTR text

Three representations are kept explicitly distinct
(`medical_stt/processing/bidi.py`):

- **logical** — the transcript exactly as recognized/normalized. This is
  the only representation used for terminology processing and the only one
  that should ever be treated as "the transcript."
- **display** — a visual-order string for the Tkinter overlay (which does
  not implement the Unicode Bidi Algorithm itself).
- **injected** — the logical string plus **at most one** leading
  directional mark. The system does **not** wrap an entire mixed sentence
  in RLE…PDF: real target applications (Word, browsers, EMR forms) already
  run the full Unicode Bidi Algorithm on pasted text, and forcing an
  embedding around the whole string previously corrupted the visual order
  of embedded numbers/units/Latin abbreviations in exactly the kind of
  sentence this system needs to handle correctly, e.g. `فشار خون 120/80
  mmHg`, `MI در ECG مشاهده شد`, `HbA1c برابر 7.2 درصد است`, `TKA سمت راست`.

This is **not a claim of universal BiDi correctness** for every possible
target application — behavior still depends on how the receiving app
implements its own bidi handling — but the logical content is always
preserved exactly, and numeric/Latin runs are never reordered by this
system itself.

## Windows text injection

The Windows backend (`medical_stt/injection/_windows_backend.py`) uses
native `SendInput` and Win32 clipboard APIs directly via `ctypes` — no
PyAutoGUI/PyWin32 dependency on Windows. Notable correctness properties,
all covered by `tests/test_injection.py` via a `DryRunBackend`:

- Explicit 64-bit-safe `ctypes` prototypes for every Win32 call (a common
  bug: unset `restype` truncates 64-bit handles/pointers to 32 bits).
- UTF-16 code-unit-accurate backspacing (a surrogate pair, e.g. an emoji,
  counts as 2, not 1).
- ZWNJ/combining-mark-safe delta computation: streaming revisions never
  split a Persian ZWNJ join or a combining diacritic.
- Clipboard ownership handled correctly (`SetClipboardData` transfers
  ownership; freed only on failure) and verified after write, with a retry
  before falling back to reporting failure.
- Modifier hygiene: stray Ctrl/Shift/Alt/Win keys are released before
  synthesizing Ctrl+V and restored afterward.
- Long text is batched into `SendInput` chunks and the accepted event count
  is verified, since a single oversized call can be partially dropped by
  the target thread's input queue.

The overlay's shutdown lifecycle was also fixed: `close()` previously set
its "closed" flag *before* scheduling the Tk `destroy()` callback, and the
scheduler refused to run anything once that flag was set — so the window
was silently never destroyed. `close()` now schedules destruction first
(see `tests/test_overlay.py` for a regression test using a fake Tk root, no
real display required).

## Deepgram / streaming

- SDK pinned to `deepgram-sdk==7.9.0`, tested against the
  `client.listen.v1.connect(...)` call shape used here (Nova-3,
  `interim_results`, `endpointing`, `utterance_end_ms`, `smart_format`,
  `punctuate`, `keyterm`). Keyterms are passed as a list of separate
  values, as required by the current API.
- Configuration is validated (`STTProvider.validate_config()`) before any
  connection is opened.
- Errors are classified into `ErrorCategory` (`stt/base.py`): `AUTH`,
  `CONFIG`, `RATE_LIMIT`, `NETWORK`, `TIMEOUT`, `SERVER_DISCONNECT`,
  `MICROPHONE`, `SHUTDOWN`, `UNKNOWN`. **`AUTH` and `CONFIG` are never
  retried** — see `stt/reconnect.py` and `tests/test_streaming.py`.
  Everything else uses exponential backoff with bounded jitter
  (`reconnect_backoff_max`, `reconnect_jitter`).

## Audio pipeline

- The microphone callback (`app.py::_on_audio`) stays lightweight: it never
  blocks, never does string/YAML work, and never raises into sounddevice's
  real-time thread.
- `medical_stt/audio/queue.py` is a bounded queue with drop accounting:
  `stats()` exposes `depth`, `max_depth`, `dropped_total`, `put_total`,
  `get_total`. A dropped chunk is never silent — it's logged (rate-limited)
  as a warning.
- Latency instrumentation (`app.py::LatencyTracker`) logs stage durations
  (terminology, BiDi, injection, total) at INFO/DEBUG level — **never**
  transcript content.

## Logging

Standard `logging` is used throughout (`medical_stt.*` loggers). The
application **never logs**:

- API keys (not even at DEBUG level — they are never passed to a log call
  anywhere in the codebase),
- clipboard contents,
- full medical transcript text by default (only stage timings and
  event/error metadata).

## Testing

```bash
pytest tests/ -v
```

```
tests/
├── test_normalize.py       # Unicode/digit/ZWNJ/whitespace normalization
├── test_terminology.py      # safety categorization & guardrails
├── test_fst.py               # longest-match correctness
├── test_numbers.py            # numeric-expression protection
├── test_negation.py            # negation/fidelity protection
├── test_bidi.py                  # logical/display/injected separation
├── test_config.py                  # settings validation, malformed YAML
├── test_streaming.py                 # error classification & backoff policy
├── test_injection.py                   # TextInjector via DryRunBackend
├── test_audio_queue.py                   # drop accounting
├── test_overlay.py                         # shutdown lifecycle (no real GUI)
├── test_app.py                               # end-to-end pipeline, fake provider
├── regression/
│   └── test_medical_cases.py                   # semantic-preservation corpus
└── fixtures/
    └── medical_regression_corpus.yaml
```

No test requires a real Deepgram API key, a real Windows GUI, or a real
audio device — the provider, injection backend, and overlay are all
exercised through fakes/mocks (`FakeProvider`, `DryRunBackend`,
`enabled=False` overlay).

## Security

See [`SECURITY.md`](SECURITY.md) for the full incident report. Summary:

- A real Deepgram API key was previously committed in `.env` and must be
  treated as compromised — **revoke it in the Deepgram console and issue a
  new one**; this repository does not (and will not) contain a replacement
  credential.
- `.env` is now git-ignored; `.env.example` contains placeholders only.
- The exposed key was removed from git history.
- No API keys, clipboard contents, or full transcripts are logged.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on Python 3.10/3.11/3.12:
dependency install, the full test suite (mocked provider/network, no real
credentials), `ruff` lint, `mypy` type check, and a secret-scanning job
(gitleaks) over the repository history.

## Limitations

- This is **not** a certified medical device and makes **no guarantee of
  clinical accuracy**. It is a deterministic dictation aid; a human must
  review the final text before it becomes part of a medical record.
- The terminology engine's longest-match behavior is deterministic and
  tested (see `tests/test_fst.py`), but the *correctness of individual
  rules* in `data/corrections.yaml` depends on the rule's own
  category/confidence — `context_dependent` and `unsafe` rules are
  intentionally not applied by default.
- BiDi handling preserves logical content and avoids the previous
  whole-string RLE/PDF corruption bug, but it is **not a universal
  guarantee** that every target application will visually render every
  possible mixed string identically; behavior depends on that
  application's own bidi implementation.
- Negation protection (`processing/negation.py`) is a small, fixed marker
  list, not a clinical NLP negation-scope detector. It prevents the
  terminology engine from rewriting *inside* a recognized negation phrase;
  it does not understand negation scope beyond that phrase.
- Windows-native injection (`ctypes` SendInput/clipboard) is the primary,
  most-tested path. The Linux/macOS fallback (`pyautogui`/`pyperclip`) is
  less exercised in real-world dictation and is provided for development
  convenience.
- No LLM is used in the transcription-correction path, by design; this
  means the system will not "understand" novel phrasing outside its
  explicit rule set — it will correctly leave it unchanged rather than
  guess.
