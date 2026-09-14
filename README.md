# Medical STT

Real-time Persian + English medical dictation for Windows.

Microphone → Deepgram Nova-3 → light normalization → Finite State Transducer (medical glossary) → floating overlay + automatic injection into the focused window.

## Features

- Streaming ASR with Deepgram (Nova-3, Persian)
- Deterministic post-processing only (no LLMs)
- Large external medical glossary (abbreviations, anatomy, procedures, labs, drugs)
- Proper Persian / RTL injection on Windows (native Win32 SendInput + clipboard)
- Always-on-top overlay with RTL/LTR awareness
- Automatic reconnection on network or API errors
- All medical terms and keyterms live in YAML — edit without touching code

## Requirements

- Windows 10/11 (primary target; Linux/macOS work with clipboard fallback)
- Python 3.10+
- Deepgram API key
- Microphone

## Install

```bash
cd medical_stt
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS

pip install -r requirements.txt
copy .env.example .env          # then edit .env and set DEEPGRAM_API_KEY
```

## Run

```bash
python run.py
```

Place the cursor in any text field (EMR, Word, browser form, etc.) and speak. Final utterances are corrected and pasted automatically.

Stop with `Ctrl+C`.

## Configuration

| File | Purpose |
|------|---------|
| `.env` | `DEEPGRAM_API_KEY` (required) |
| `config/settings.yaml` | Model, language, inject mode, reconnection, overlay |
| `data/keyterms.yaml` | Deepgram keyterm boosting list |
| `data/corrections.yaml` | FST rewrite rules (`from` → `to`) |

### Inject modes

- `paste` (default) — clipboard + Ctrl+V. Best for Persian BiDi shaping in most apps.
- `type` — Unicode key events. Useful when the target app ignores the clipboard.

### Adding medical terms

Edit `data/corrections.yaml`:

```yaml
rules:
  - from: "آی سی یو"
    to: "ICU"
  - from: "سکته قلبی"
    to: "MI"
  - from: "تعویض کامل مفصل زانو"
    to: "TKA"
```

Also add high-value terms to `data/keyterms.yaml` so Deepgram hears them more reliably.

The FST always prefers the **longest** matching source string, so more specific phrases win over shorter ones.

## Project layout

```
medical_stt/
├── run.py                 # entry point
├── requirements.txt
├── .env.example
├── config/
│   └── settings.yaml
├── data/
│   ├── keyterms.yaml
│   └── corrections.yaml
└── medical_stt/
    ├── app.py             # main loop + Deepgram + reconnection
    ├── config.py          # load settings & YAML rules
    ├── fst.py             # pure-Python FST
    ├── normalize.py       # light Arabic→Persian / spacing cleanup
    ├── injector.py        # Windows-first text injection
    └── overlay.py         # always-on-top transcript window
```

## Notes

- Overlay uses Tkinter (ships with standard Python on Windows). If unavailable, the app falls back to console-only mode.
- Clipboard is restored after each paste when `restore_clipboard: true`.
- On non-Windows platforms the injector uses `pyperclip` + `pyautogui` as a fallback.
