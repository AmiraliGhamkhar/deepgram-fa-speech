# 12 — Module review: `injection/` (Windows text injection)

Read in full: backend.py L1–133, text_injector.py L1–208,
_windows_backend.py L1–386, _fallback_backend.py L1–75. Cross-read:
processing/bidi.py (module 10), medical_stt/app.py L245–249 (caller).

## What it holds

`InjectionBackend` abstraction + DryRun/Failing test backends;
`TextInjector` (streaming delta backspacing with grapheme-safe split points,
UTF-16-correct backspace counts, BiDi-aware paste); `WindowsBackend`
(SendInput + Win32 clipboard); pyautogui/pyperclip fallback (dev only).

## Findings

**F1 · Info · Clinical text transits the Windows clipboard by design, and
restore-failure is silent.** FACT: `_windows_backend.py:328–386` — the
utterance is placed on the clipboard, Ctrl+V synthesized, previous content
restored after `settle_seconds` (L378–381). If the restore
`_set_clipboard_text(previous)` fails (clipboard busy), its return value is
ignored (L381) — the user's clipboard is then left holding dictated medical
text with no log line. Inherent to paste-mode injection; the gap is only the
silent restore failure. A warning log there would make support's job
possible.

**F2 · Info · Double application of `to_injected` is safe by idempotence.**
FACT: app.py:246 computes `to_injected(rewritten)` and passes the result to
`TextInjector.paste_text` (text_injector.py:163), which applies
`to_injected` again (L183). The second call is a no-op because
`to_injected` refuses to stack marks (`bidi.py:109–110` checks the leading
RLM/LRM). Correct, but subtle — the `add_rtl_mark` parameter is dead
documentation ("now equivalent to always-on", L173–175) and could be
removed to kill the ambiguity.

**F3 · Info · `_safe_split_point` handles the `"" in str` Python trap
explicitly** (text_injector.py:123–152, the `bool(ch) and` guard at
L136–140 with an explanatory NB). Recording because this exact trap
otherwise silently produces spurious backspaces.

## Correct in this module (verified)

- **No shell, no ffmpeg, no subprocess anywhere in the injection path** —
  input synthesis is `SendInput` with explicit argtypes/restypes for every
  64-bit-unsafe function (handle-truncation class documented and prevented,
  _windows_backend.py:105–156; the same class is handled in dpapi.py, see
  module 06 F1). No injection surface for transcript content: it is data,
  never a command.
- Clipboard ownership: `GlobalAlloc(GMEM_MOVEABLE)` → on success
  `h_mem = None` (system owns it), on failure `GlobalFree` in `finally`
  (L299–326) — the classic leak/double-free trap is handled.
- Clipboard-busy: `OpenClipboard` retried with backoff, 12 attempts
  (L256–274), because a single miss dropped whole sentences (documented at
  backend.py docstring note 3).
- Paste correctness: after setting the clipboard, content is read back and
  compared; a mismatch aborts the paste rather than typing the previous
  owner's content (L336–352) — and the comment explains why it must not
  "fall back to typing" (double injection).
- Modifier hygiene: physically-held Ctrl/Shift/Alt/Win are detected via
  `GetAsyncKeyState`, released before Ctrl+V, restored after
  (L215–253) — prevents Ctrl+Shift+V misfires in EMR forms.
- Long-text safety: SendInput batched (80 events) with accepted-count
  verification (L166–188); Persian keyboard layouts handled by sending the
  *virtual key* V, not the layout character (L246–249 comment).
- Streaming deltas: backspace counts are UTF-16 code units, not code points
  (text_injector.py:117–121), so non-BMP characters erase correctly
  (backend.py docstring note 6); split points back off ZWNJ/ZWJ, Arabic
  combining marks, and dangling surrogates (L123–152).
- Whitespace normalization preserves line structure (the "new line" contract
  for structured dictation) while collapsing horizontal runs
  (text_injector.py:20–47).
- No transcript text is ever logged in any of the four files (checked every
  log call: errors/counts only).

## Summary (3 lines)

This is the most bug-history-dense subsystem and it shows in the right way:
every documented past failure (64-bit truncation, clipboard races, modifier
stuck keys, UTF-16 backspaces, input-queue truncation) has a matching guard.
No shell/ffmpeg injection risk exists; the only notes are the silently-
failed clipboard restore (PHI lingers) and dead `add_rtl_mark` plumbing.
