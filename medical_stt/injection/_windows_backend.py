"""Native Win32 SendInput + clipboard backend.

Importing this module on a non-Windows OS raises `RuntimeError` immediately;
it is only imported lazily by `get_default_backend()` when
`platform.system() == "Windows"`.
"""
from __future__ import annotations

import ctypes
import logging
import platform
import time
from ctypes import wintypes
from typing import List, Optional

from .backend import InjectionBackend, _INJECT_SIGNATURE

log = logging.getLogger("medical_stt.injection.windows")

if platform.system().lower() != "windows":  # pragma: no cover
    raise RuntimeError("_windows_backend can only be imported on Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

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

_INTERFERING_MODIFIERS = (VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN)


def _key_input(vk: int, scan: int, flags: int) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki = KEYBDINPUT(vk, scan, flags, 0, _INJECT_SIGNATURE)
    return inp


def _unicode_input(code_unit: int, key_up: bool) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if key_up else 0)
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki = KEYBDINPUT(0, code_unit, flags, 0, _INJECT_SIGNATURE)
    return inp


class WindowsBackend(InjectionBackend):
    def __init__(self) -> None:
        pass

    # -- keystrokes -----------------------------------------------------

    def _send_inputs(self, inputs: List[INPUT], chunk: int = 80) -> bool:
        """Send events in small batches.

        A single huge SendInput call can be partially dropped by the
        target thread's input queue, which silently truncated long Persian
        sentences. Batching (and verifying the accepted count) makes that
        visible and rare.
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
                err = ctypes.get_last_error()  # type: ignore[attr-defined]
                log.warning("SendInput accepted %d/%d events (error %s)", sent, n, err)
                return False
            if len(inputs) > chunk:
                time.sleep(0.001)
        return total_sent == len(inputs)

    def send_unicode_text(self, text: str) -> bool:
        utf16_bytes = text.encode("utf-16-le")
        code_units = [
            int.from_bytes(utf16_bytes[i:i + 2], "little")
            for i in range(0, len(utf16_bytes), 2)
        ]
        inputs: List[INPUT] = []
        for code in code_units:
            inputs.append(_unicode_input(code, key_up=False))
            inputs.append(_unicode_input(code, key_up=True))
        return self._send_inputs(inputs)

    def send_backspaces(self, count: int) -> bool:
        if count <= 0:
            return True
        scan = user32.MapVirtualKeyW(VK_BACK, MAPVK_VK_TO_VSC)
        inputs: List[INPUT] = []
        for _ in range(count):
            inputs.append(_key_input(VK_BACK, scan, 0))
            inputs.append(_key_input(VK_BACK, scan, KEYEVENTF_KEYUP))
        return self._send_inputs(inputs)

    # -- modifier hygiene -------------------------------------------------

    def _stuck_modifiers(self) -> List[int]:
        return [vk for vk in _INTERFERING_MODIFIERS if user32.GetAsyncKeyState(vk) & 0x8000]

    def _set_modifier(self, vk: int, pressed: bool) -> None:
        scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
        flags = 0 if pressed else KEYEVENTF_KEYUP
        if vk in (VK_LWIN, VK_RWIN):
            flags |= KEYEVENTF_EXTENDEDKEY
        self._send_inputs([_key_input(vk, scan, flags)])

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
            _key_input(VK_CONTROL, ctrl_scan, 0),
            _key_input(VK_V, v_scan, 0),
            _key_input(VK_V, v_scan, KEYEVENTF_KEYUP),
            _key_input(VK_CONTROL, ctrl_scan, KEYEVENTF_KEYUP),
        ]
        ok = self._send_inputs(sequence)

        for vk in stuck:
            if user32.GetAsyncKeyState(vk) & 0x8000:
                continue
            self._set_modifier(vk, pressed=True)

        return ok

    # -- clipboard ----------------------------------------------------------

    def _open_clipboard(self, attempts: int = 12, delay: float = 0.02) -> bool:
        """OpenClipboard fails while another process holds it open. A
        single try meant a whole dictated sentence vanished, so retry
        briefly."""
        hwnd = user32.GetForegroundWindow()
        for i in range(attempts):
            if user32.OpenClipboard(hwnd):
                return True
            if user32.OpenClipboard(None):
                return True
            time.sleep(delay * (1 + i * 0.25))
        log.warning("clipboard busy — another application is holding it open")
        return False

    def get_clipboard_text(self) -> Optional[str]:
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
        except OSError as exc:
            log.debug("clipboard read failed: %s", exc)
            return None
        finally:
            user32.CloseClipboard()

    def _set_clipboard_text(self, text: str) -> bool:
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

            # On success the system takes ownership of h_mem; we must not
            # free it. On failure we still own it and must release it
            # ourselves.
            if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
                return False

            h_mem = None  # ownership transferred
            return True
        finally:
            if h_mem:
                kernel32.GlobalFree(h_mem)
            user32.CloseClipboard()

    def paste_text(self, text: str, restore_clipboard: bool, settle_seconds: float) -> bool:
        previous: Optional[str] = None
        if restore_clipboard:
            previous = self.get_clipboard_text()

        if not self._set_clipboard_text(text):
            return False

        # Confirm the text really landed before pressing Ctrl+V; otherwise
        # we would paste whatever the previous owner left behind.
        if self.get_clipboard_text() != text:
            time.sleep(0.03)
            if not self._set_clipboard_text(text):
                return False
            if self.get_clipboard_text() != text:
                # Report failure instead of typing here: the caller owns
                # the fallback, and doing it in both places double-injects
                # text.
                log.warning("clipboard verification failed; aborting paste")
                return False

        ok = self._send_paste_keystroke()

        # Let the target application actually read the clipboard. Pasting
        # is asynchronous: returning immediately let the NEXT utterance
        # overwrite the clipboard mid-read, which duplicated or dropped
        # sentences.
        if settle_seconds:
            time.sleep(settle_seconds)

        if restore_clipboard and previous is not None and previous != text:
            self._set_clipboard_text(previous)

        return ok
