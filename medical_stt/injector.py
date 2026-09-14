"""
Cross-platform text injection focused on Windows with full support for:
- Persian/Arabic Unicode (گ، چ، پ، ژ، ی، ک)
- Zero-Width Non-Joiner (نیم‌فاصله \\u200C)
- RTL (Right-to-Left) text direction handling
- Real-time STT hypothesis revisions (smart backspacing)
- Native UTF-16 SendInput and Win32 Clipboard (No external dependencies on Windows)

Correctness notes (these were real injection bugs):

1. ctypes defaults EVERY function's restype to a 32-bit C ``int``. On 64-bit
   Windows ``GlobalAlloc``/``GlobalLock``/``SetClipboardData`` return 64-bit
   handles/pointers, so the high 32 bits were silently truncated. ``memmove``
   then wrote the UTF-16 text to a bogus address and ``SetClipboardData``
   received a garbage handle -> the paste inserted stale/empty/corrupt text
   even though the transcript itself was perfect. Every prototype below is now
   declared explicitly.
2. ``SetClipboardData`` transfers OWNERSHIP of the HGLOBAL to the system. The
   block must not be freed on success, and must not be locked when handed over.
3. ``OpenClipboard`` routinely fails with ERROR_ACCESS_DENIED because another
   process (Office, browsers, clipboard managers) holds the clipboard open for
   a few milliseconds. A single attempt turned into a dropped utterance, so it
   is retried with a short backoff.
4. Ctrl+V was synthesized while the user's real modifier keys (Ctrl/Shift/Alt/
   Win) could still be physically or logically down. Ctrl+Shift+V / Ctrl+Alt+V
   are completely different commands in most editors and EMR web forms, which
   pasted nothing or opened a dialog. Stray modifiers are now released first
   and restored afterwards.
5. The clipboard was overwritten by the next utterance before the target app
   had finished reading it (paste is asynchronous), which produced duplicated
   or missing sentences during fast dictation. Injection now waits for the
   paste to be consumed before returning/restoring.
6. Backspacing counted Python code points while Windows deletes UTF-16 code
   units, so any non-BMP character erased too little.
"""
from __future__ import annotations

import platform
import time
from typing import List, Optional, Tuple

_SYSTEM = platform.system().lower()

# Tag our own synthetic events so we can tell them apart from real typing.
_INJECT_SIGNATURE = 0x53545449  # "STTI"

# Win32 Native Setup (No PyQt/PyAutoGUI required on Windows)
if _SYSTEM == "windows":
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # Constants
    INPUT_KEYBOARD = 1
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_SCANCODE = 0x0008
    VK_CONTROL = 0x11
    VK_SHIFT = 0x10
    VK_MENU = 0x12  # Alt
    VK_LWIN = 0x5B
    VK_RWIN = 0x5C
    VK_BACK = 0x08
    VK_V = 0x56
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    ERROR_ACCESS_DENIED = 5
    MAPVK_VK_TO_VSC = 0

    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class _INPUTunion(ctypes.Union):
        _fields_ = [
            ("ki", KEYBDINPUT),
            ("mi", MOUSEINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [
            ("type", wintypes.DWORD),
            ("union", _INPUTunion),
        ]

    # ---- Explicit prototypes: mandatory for 64-bit handle/pointer safety ----
    HGLOBAL = wintypes.HGLOBAL if hasattr(wintypes, "HGLOBAL") else ctypes.c_void_p

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = HGLOBAL

    kernel32.GlobalLock.argtypes = [HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID

    kernel32.GlobalUnlock.argtypes = [HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    kernel32.GlobalFree.argtypes = [HGLOBAL]
    kernel32.GlobalFree.restype = HGLOBAL

    kernel32.GlobalSize.argtypes = [HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL

    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL

    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL

    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = HGLOBAL

    user32.SetClipboardData.argtypes = [wintypes.UINT, HGLOBAL]
    user32.SetClipboardData.restype = HGLOBAL

    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL

    user32.GetClipboardSequenceNumber.argtypes = []
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD

    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT

    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short

    user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    user32.MapVirtualKeyW.restype = wintypes.UINT

    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND


class TextInjector:
    """Injects Persian/RTL and Unicode text at current cursor position."""

    #: Modifiers that silently turn Ctrl+V into a different command.
    _INTERFERING_MODIFIERS = ("VK_SHIFT", "VK_MENU", "VK_LWIN", "VK_RWIN")

    def __init__(
        self,
        dry_run: bool = False,
        enable_smart_rewrite: bool = True,
        restore_clipboard: bool = True,
        paste_settle_seconds: float = 0.12,
    ):
        self.dry_run = dry_run
        self.enable_smart_rewrite = enable_smart_rewrite
        self.restore_clipboard = restore_clipboard
        #: How long to let the target app consume the clipboard after Ctrl+V.
        self.paste_settle_seconds = max(0.0, paste_settle_seconds)
        self._last_partial: str = ""

    def reset_partial(self) -> None:
        """Reset the streaming state (call when a sentence is finalized)."""
        self._last_partial = ""

    def type_delta_from_partial(self, partial: str) -> None:
        """
        Handles real-time speech streaming.
        If STT revises previous Persian words, it sends Backspaces and types the new text.
        """
        partial = partial or ""
        if partial == self._last_partial:
            return

        if not self.enable_smart_rewrite:
            # Simple append-only mode
            if partial.startswith(self._last_partial):
                delta = partial[len(self._last_partial):]
                self._last_partial = partial
                if delta:
                    self.type_text(delta)
            return

        # Find common prefix between previous hypothesis and new hypothesis.
        common_len = 0
        min_len = min(len(self._last_partial), len(partial))
        while common_len < min_len and self._last_partial[common_len] == partial[common_len]:
            common_len += 1

        # Never split a surrogate pair or orphan a combining/ZWNJ sequence.
        common_len = self._safe_split_point(self._last_partial, partial, common_len)

        removed = self._last_partial[common_len:]
        delta = partial[common_len:]

        # Windows deletes UTF-16 code units, not Python code points.
        backspaces_needed = self._utf16_len(removed)

        self._last_partial = partial

        if backspaces_needed > 0:
            self.send_backspaces(backspaces_needed)
            time.sleep(0.01)

        if delta:
            self.type_text(delta)

    @staticmethod
    def _utf16_len(text: str) -> int:
        """Number of UTF-16 code units, i.e. how many backspaces Windows needs."""
        return len(text.encode("utf-16-le")) // 2

    @staticmethod
    def _safe_split_point(old: str, new: str, index: int) -> int:
        """
        Back the common-prefix boundary off any position that would cut a
        grapheme in half (ZWNJ joins, combining marks, surrogate halves).
        """
        combining = "\u200c\u200d\u064b\u064c\u064d\u064e\u064f\u0650\u0651\u0652\u0670"

        def joins(ch: str) -> bool:
            # NB: `"" in combining` is True in Python, so the empty
            # end-of-string case must be excluded explicitly or a plain
            # append would be treated as a mid-grapheme cut and emit a
            # bogus backspace.
            return bool(ch) and ch in combining

        while index > 0:
            prev = old[index - 1]
            nxt_old = old[index] if index < len(old) else ""
            nxt_new = new[index] if index < len(new) else ""
            if joins(prev) or joins(nxt_old) or joins(nxt_new):
                index -= 1
                continue
            if "\ud800" <= prev <= "\udbff":  # dangling high surrogate
                index -= 1
                continue
            break
        return index

    def type_text(self, text: str) -> bool:
        """Type text character-by-character via Unicode events."""
        if not text:
            return False
        if self.dry_run:
            print(f"  [DRY_RUN type] {text!r}")
            return True

        try:
            if _SYSTEM == "windows":
                return self._type_windows(text)
            return self._type_fallback(text)
        except Exception as e:
            print(f"  [inject error] {e}")
            return False

    def paste_text(self, text: str, add_rtl_mark: bool = False) -> bool:
        """
        Instantly pastes text via Clipboard (Ctrl+V).
        Recommended for complete sentences to ensure proper BiDi shaping in target apps.
        """
        if not text:
            return False

        # Optional: Add Right-to-Left Mark (RLM \u200F) for apps that default to LTR
        if add_rtl_mark and not text.startswith("\u200f"):
            text = "\u200f" + text

        if self.dry_run:
            print(f"  [DRY_RUN paste] {text!r}")
            return True

        try:
            if _SYSTEM == "windows":
                return self._paste_windows(text)
            return self._paste_fallback(text)
        except Exception as e:
            print(f"  [paste error] {e}")
            return False

    def send_backspaces(self, count: int) -> bool:
        """Send N backspace keystrokes to erase revised text."""
        if count <= 0:
            return True
        if self.dry_run:
            print(f"  [DRY_RUN backspace x{count}]")
            return True

        if _SYSTEM == "windows":
            scan = user32.MapVirtualKeyW(VK_BACK, MAPVK_VK_TO_VSC)
            inputs: List[INPUT] = []
            for _ in range(count):
                inputs.append(self._key_input(VK_BACK, scan, 0))
                inputs.append(self._key_input(VK_BACK, scan, KEYEVENTF_KEYUP))
            return self._send_inputs(inputs)

        try:
            import pyautogui

            for _ in range(count):
                pyautogui.press("backspace")
            return True
        except Exception:
            return False

    # ------------------ Windows Specific Low-Level Logic ------------------

    @staticmethod
    def _key_input(vk: int, scan: int, flags: int) -> "INPUT":
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki = KEYBDINPUT(vk, scan, flags, 0, _INJECT_SIGNATURE)
        return inp

    @staticmethod
    def _unicode_input(code_unit: int, key_up: bool) -> "INPUT":
        flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if key_up else 0)
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki = KEYBDINPUT(0, code_unit, flags, 0, _INJECT_SIGNATURE)
        return inp

    def _send_inputs(self, inputs: List["INPUT"], chunk: int = 80) -> bool:
        """
        Send events in small batches.

        A single huge SendInput call can be partially dropped by the target
        thread's input queue, which silently truncated long Persian sentences.
        Batching (and verifying the accepted count) makes that visible and rare.
        """
        if not inputs:
            return True
        total_sent = 0
        for start in range(0, len(inputs), chunk):
            batch = inputs[start:start + chunk]
            n = len(batch)
            arr = (INPUT * n)(*batch)
            sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
            total_sent += sent
            if sent != n:
                err = ctypes.get_last_error()
                print(f"  [inject warn] SendInput accepted {sent}/{n} events (error {err})")
                return False
            if len(inputs) > chunk:
                time.sleep(0.001)
        return total_sent == len(inputs)

    def _type_windows(self, text: str) -> bool:
        """
        Sends UTF-16 code units via SendInput.
        Handles Persian letters, ZWNJ, and UTF-16 surrogate pairs properly.
        """
        utf16_bytes = text.encode("utf-16-le")
        code_units = [
            int.from_bytes(utf16_bytes[i:i + 2], "little")
            for i in range(0, len(utf16_bytes), 2)
        ]

        inputs: List[INPUT] = []
        for code in code_units:
            inputs.append(self._unicode_input(code, key_up=False))
            inputs.append(self._unicode_input(code, key_up=True))

        return self._send_inputs(inputs)

    # -- modifier hygiene ---------------------------------------------------

    def _stuck_modifiers(self) -> List[int]:
        """Modifiers currently down that would corrupt a synthetic Ctrl+V."""
        down = []
        for name in self._INTERFERING_MODIFIERS:
            vk = globals()[name]
            if user32.GetAsyncKeyState(vk) & 0x8000:
                down.append(vk)
        return down

    def _set_modifier(self, vk: int, pressed: bool) -> None:
        scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
        flags = 0 if pressed else KEYEVENTF_KEYUP
        if vk in (VK_LWIN, VK_RWIN):
            flags |= KEYEVENTF_EXTENDEDKEY
        self._send_inputs([self._key_input(vk, scan, flags)])

    def _paste_windows(self, text: str) -> bool:
        """
        Direct Win32 Unicode clipboard injection + Ctrl+V synthesis.
        Guarantees zero character corruption for complex Persian text.
        """
        previous: Optional[str] = None
        if self.restore_clipboard:
            previous = self._get_windows_clipboard()

        if not self._set_windows_clipboard(text):
            return False

        # Confirm the text really landed before pressing Ctrl+V; otherwise we
        # would paste whatever the previous owner left behind.
        if self._get_windows_clipboard() != text:
            time.sleep(0.03)
            if not self._set_windows_clipboard(text):
                return False
            if self._get_windows_clipboard() != text:
                # Report failure instead of typing here: the caller owns the
                # fallback, and doing it in both places double-injected text.
                print("  [paste warn] clipboard verification failed")
                return False

        ok = self._send_paste_keystroke()

        # Let the target application actually read the clipboard. Pasting is
        # asynchronous: returning immediately let the NEXT utterance overwrite
        # the clipboard mid-read, which duplicated or dropped sentences.
        if self.paste_settle_seconds:
            time.sleep(self.paste_settle_seconds)

        if self.restore_clipboard and previous is not None and previous != text:
            self._set_windows_clipboard(previous)

        return ok

    def _send_paste_keystroke(self) -> bool:
        """Synthesize a clean Ctrl+V with no foreign modifiers attached."""
        stuck = self._stuck_modifiers()
        for vk in stuck:
            self._set_modifier(vk, pressed=False)
        if stuck:
            time.sleep(0.01)

        ctrl_scan = user32.MapVirtualKeyW(VK_CONTROL, MAPVK_VK_TO_VSC)
        v_scan = user32.MapVirtualKeyW(VK_V, MAPVK_VK_TO_VSC)

        # NOTE: VK_V (not the layout-dependent character) — under a Persian
        # keyboard layout the physical V key produces "ر", but the paste
        # accelerator is bound to the virtual key, so this stays correct.
        sequence = [
            self._key_input(VK_CONTROL, ctrl_scan, 0),
            self._key_input(VK_V, v_scan, 0),
            self._key_input(VK_V, v_scan, KEYEVENTF_KEYUP),
            self._key_input(VK_CONTROL, ctrl_scan, KEYEVENTF_KEYUP),
        ]
        ok = self._send_inputs(sequence)

        # Restore whatever the user was genuinely holding down.
        for vk in stuck:
            if user32.GetAsyncKeyState(vk) & 0x8000:
                continue
            self._set_modifier(vk, pressed=True)

        return ok

    # -- clipboard ----------------------------------------------------------

    def _open_clipboard(self, attempts: int = 12, delay: float = 0.02) -> bool:
        """
        OpenClipboard fails while another process holds it open. A single try
        meant a whole dictated sentence vanished, so retry briefly.
        """
        hwnd = user32.GetForegroundWindow()
        for i in range(attempts):
            if user32.OpenClipboard(hwnd):
                return True
            if user32.OpenClipboard(None):
                return True
            time.sleep(delay * (1 + i * 0.25))
        print("  [clipboard] busy — another application is holding it open")
        return False

    def _get_windows_clipboard(self) -> Optional[str]:
        """Read CF_UNICODETEXT, or None when unavailable/non-text."""
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        if not self._open_clipboard():
            return None
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return ctypes.c_wchar_p(ptr).value
            finally:
                kernel32.GlobalUnlock(handle)
        except Exception:
            return None
        finally:
            user32.CloseClipboard()

    def _set_windows_clipboard(self, text: str) -> bool:
        """Sets CF_UNICODETEXT directly into Windows Clipboard."""
        text_bytes = (text + "\0").encode("utf-16-le")
        size = len(text_bytes)

        if not self._open_clipboard():
            return False

        h_mem = None
        try:
            if not user32.EmptyClipboard():
                return False

            h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not h_mem:
                return False

            p_mem = kernel32.GlobalLock(h_mem)
            if not p_mem:
                return False

            ctypes.memmove(p_mem, text_bytes, size)
            kernel32.GlobalUnlock(h_mem)

            # On success the system takes ownership of h_mem; we must not free
            # it. On failure we still own it and must release it ourselves.
            if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
                return False

            h_mem = None  # ownership transferred
            return True
        finally:
            if h_mem:
                kernel32.GlobalFree(h_mem)
            user32.CloseClipboard()

    # ------------------ Non-Windows Fallbacks ------------------

    def _type_fallback(self, text: str) -> bool:
        # PyAutoGUI's write() cannot emit non-ASCII on X11/macOS, so Persian
        # came out as dropped characters. Always go through the clipboard.
        return self._paste_fallback(text)

    def _paste_fallback(self, text: str) -> bool:
        import pyautogui
        import pyperclip

        previous = None
        if self.restore_clipboard:
            try:
                previous = pyperclip.paste()
            except Exception:
                previous = None

        pyperclip.copy(text)
        time.sleep(0.05)

        if _SYSTEM == "darwin":
            pyautogui.hotkey("command", "v")
        else:
            pyautogui.hotkey("ctrl", "v")

        if self.paste_settle_seconds:
            time.sleep(self.paste_settle_seconds)

        if previous is not None and previous != text:
            try:
                pyperclip.copy(previous)
            except Exception:
                pass
        return True
