"""Opt-in native input check in a dedicated Windows test window.

Run `python scripts/windows_injection_check.py --interactive` on Windows.
Click a test button, leave this window focused, and inspect the Persian/Latin
text visually. No microphone, network, credentials or patient text is used.
Paste tests preserve supported plain-text/empty clipboards; rich-format
clipboards cause a safe refusal. This is not an automated EMR compatibility
certification and never runs as part of unattended CI.
"""
from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SAMPLE = "آزمون پزشکی BP 120/80 — 5 mg"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interactive", action="store_true", help="explicitly allow native input in the test window")
    args = parser.parse_args()
    if sys.platform != "win32" or not args.interactive:
        print("Requires Windows and --interactive; no native input was sent.")
        return 2

    import tkinter as tk
    from ctypes import wintypes
    from medical_stt.injection._windows_backend import WindowsBackend, user32
    from medical_stt.processing.bidi import to_injected

    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    root = tk.Tk()
    root.title("Medical STT — native input check (synthetic text only)")
    tk.Label(root, text="Keep this test window focused. Do not change focus during a test.").pack()
    field = tk.Text(root, width=65, height=8)
    field.pack()
    status = tk.StringVar(value="Choose a test. Your clipboard is not changed until you choose paste.")
    tk.Label(root, textvariable=status, wraplength=600).pack()
    results: queue.Queue[tuple[bool, float]] = queue.Queue(maxsize=1)
    busy = False
    expected = to_injected(SAMPLE)
    closed = threading.Event()

    def start(mode: str) -> None:
        nonlocal busy
        if busy:
            return
        busy = True
        field.delete("1.0", "end")
        field.focus_force()
        status.set("Testing; keep focus here…")
        def launch() -> None:
            # Record the test foreground window on the GUI thread, after focus.
            target = user32.GetAncestor(field.winfo_id(), 2)  # GA_ROOT, our test window only
            def worker() -> None:
                before = time.perf_counter()
                backend = WindowsBackend(allow_own_process=True, expected_target=target)
                ok = False
                try:
                    if not closed.is_set() and target == user32.GetForegroundWindow():
                        ok = (backend.paste_text(expected, True, .12) if mode == "paste"
                              else backend.send_unicode_text(expected))
                finally:
                    results.put((ok, time.perf_counter() - before))
            threading.Thread(target=worker, daemon=True, name="native-test").start()
        root.after(100, launch)

    def poll() -> None:
        nonlocal busy
        try:
            accepted, elapsed = results.get_nowait()
        except queue.Empty:
            pass
        else:
            actual = field.get("1.0", "end-1c")
            matches = actual == expected
            status.set(f"{'PASS' if accepted and matches else 'FAIL'}: backend accepted={accepted}, "
                       f"target content matches={matches}, operation={elapsed*1000:.1f} ms. "
                       "Also inspect shaping and number order visually.")
            busy = False
        if not closed.is_set():
            root.after(50, poll)

    tk.Button(root, text="Test Unicode typing", command=lambda: start("type")).pack()
    tk.Button(root, text="Test clipboard paste", command=lambda: start("paste")).pack()
    def close() -> None:
        if busy:
            status.set("Wait for the current native operation before closing.")
            return
        closed.set()
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", close)
    poll()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
