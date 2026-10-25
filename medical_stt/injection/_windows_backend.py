"""Native Win32 SendInput + clipboard backend.

Importing this module on a non-Windows OS raises `RuntimeError` immediately;
it is only imported lazily by `get_default_backend()` when
`platform.system() == "Windows"`.
"""
from __future__ import annotations

import atexit
import ctypes
import logging
import os
import platform
import threading
import time
from ctypes import wintypes
from typing import List, Optional

from .backend import CLIPBOARD_CLEARED_PLACEHOLDER, InjectionBackend, _INJECT_SIGNATURE

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
WS_POPUP = 0x80000000
#: Special parent that makes a window message-only: it can own the
#: clipboard and receive its notifications, but can never be shown.
HWND_MESSAGE = -3

#: Upper bound on one clipboard read: 1 MiB of UTF-16. Dictated utterances
#: are kilobytes; anything near this on a clinician's clipboard is not ours
#: to haul through memory (or to restore byte-for-byte).
_MAX_CLIPBOARD_CHARS = (1024 * 1024) // 2

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
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL

_INTERFERING_MODIFIERS = (VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN, VK_CONTROL)


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


# ---- clipboard owner window -------------------------------------------------
#
# `OpenClipboard` needs an owner window, and `EmptyClipboard` assigns
# clipboard ownership TO that window. This code used to pass
# `GetForegroundWindow()` -- i.e. it made the EMR application the clipboard
# owner, so that process started receiving WM_RENDERFORMAT /
# WM_DESTROYCLIPBOARD for data it never put there, and delayed rendering
# could break the paste. Passing NULL instead is documented to associate
# the clipboard with the current task, but a NULL owner makes
# `SetClipboardData` unusable for our write sequence in practice. So: one
# hidden message-only window of our own (the predefined STATIC class needs
# no class registration and its default proc absorbs clipboard messages),
# created lazily on first clipboard use, never shown, destroyed at exit.

_clipboard_owner_lock = threading.Lock()
_clipboard_owner_hwnd: Optional[object] = None
_clipboard_owner_attempted = False


def _destroy_clipboard_owner_window() -> None:
    global _clipboard_owner_hwnd
    hwnd, _clipboard_owner_hwnd = _clipboard_owner_hwnd, None
    if hwnd:
        try:
            user32.DestroyWindow(hwnd)
        except Exception:  # noqa: BLE001 - process exit; the OS reclaims it
            pass


def _clipboard_owner_window() -> Optional[object]:
    """Our process's hidden clipboard owner window, or None if it could
    not be created (write paths must then fail rather than borrow the
    foreground application's window -- see `_open_clipboard`)."""
    global _clipboard_owner_hwnd, _clipboard_owner_attempted
    with _clipboard_owner_lock:
        if _clipboard_owner_attempted:
            return _clipboard_owner_hwnd
        _clipboard_owner_attempted = True
        try:
            hwnd = user32.CreateWindowExW(
                0, "STATIC", "", WS_POPUP, 0, 0, 0, 0,
                HWND_MESSAGE, None, None, None,
            )
        except Exception as exc:  # noqa: BLE001 - no owner, writes fail closed
            log.warning("clipboard owner window unavailable (%s)", type(exc).__name__)
            return None
        if not hwnd:
            log.warning("clipboard owner window could not be created")
            return None
        _clipboard_owner_hwnd = hwnd
        atexit.register(_destroy_clipboard_owner_window)
        return hwnd


# Sequence numbers let restoration avoid overwriting a newer user copy.
user32.CountClipboardFormats.argtypes = []
user32.CountClipboardFormats.restype = ctypes.c_int
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT


class WindowsBackend(InjectionBackend):
    def __init__(self, *, allow_own_process: bool = False, expected_target: Optional[int] = None) -> None:
        # Diagnostics may explicitly target their own synthetic-text window.
        # Production must not dictate into its settings/secret/recovery fields.
        self._allow_own_process = allow_own_process
        self._expected_target = expected_target
        #: Clipboard sequence number of this backend's last successful write,
        #: or None before the first one. Initialized here -- not as a side
        #: effect of the first write -- so no read site can hit
        #: AttributeError (audit N-06).
        self._last_written_sequence: Optional[int] = None

    def _target_allowed(self, target: int) -> bool:
        if self._expected_target is not None and target != self._expected_target:
            return False
        if not target:
            # Checked first on purpose: this branch used to sit AFTER the
            # GetWindowThreadProcessId call, which already fails for a NULL
            # window -- so the one case the "focus an external document"
            # guidance exists for never produced it.
            log.warning("focus an external document before dictating")
            return False
        pid = wintypes.DWORD()
        if not user32.GetWindowThreadProcessId(target, ctypes.byref(pid)):
            return False
        if pid.value == 0:
            # A just-destroyed foreground window can still answer with a
            # thread id but no owning process. pid 0 is the idle process,
            # never a document: refuse rather than type into the void.
            log.warning("foreground window has no owning process; not injecting")
            return False
        if pid.value == os.getpid() and not self._allow_own_process:
            log.warning("focus an external document before dictating")
            return False
        return True

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
        target = user32.GetForegroundWindow()
        if not self._target_allowed(target):
            return False
        total_sent = 0
        for start in range(0, len(inputs), chunk):
            if target != user32.GetForegroundWindow():
                log.warning("focused window changed during input; remaining events cancelled")
                return False
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
        # Same hygiene as the paste keystroke: this path used to release
        # nothing, so a physically held Alt/Win during typing turned the
        # dictated characters into menu accelerators and shortcuts.
        stuck = self._release_interfering_modifiers()
        try:
            return self._send_inputs(inputs)
        finally:
            self._restore_modifiers(stuck)

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

    def _release_interfering_modifiers(self) -> List[int]:
        """Snapshot the physically held modifiers and release them all."""
        stuck = self._stuck_modifiers()
        for vk in stuck:
            self._set_modifier(vk, pressed=False)
        if stuck:
            time.sleep(0.01)
        return stuck

    def _restore_modifiers(self, stuck: List[int]) -> None:
        """Re-press only modifiers that are STILL held after injection.

        The snapshot used to be re-pressed verbatim: if the user released
        Ctrl during the few milliseconds of the paste, we pressed it again
        and left it logically stuck, so the clinician's next keystrokes
        became shortcuts. A modifier is restored only when it is both in
        the snapshot AND still reported down by a fresh `GetAsyncKeyState`
        read taken after the synthetic keyups settle.
        """
        if not stuck:
            return
        time.sleep(0.01)
        still_down = set(self._stuck_modifiers())
        for vk in stuck:
            if vk in still_down:
                self._set_modifier(vk, pressed=True)

    def _send_paste_keystroke(self) -> bool:
        """Synthesize a clean Ctrl+V with no foreign modifiers attached."""
        stuck = self._release_interfering_modifiers()

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

        self._restore_modifiers(stuck)
        return ok

    # -- clipboard ----------------------------------------------------------

    def _open_clipboard(
        self, attempts: int = 12, delay: float = 0.02, *, for_write: bool = False
    ) -> bool:
        """Open the clipboard under OUR window's ownership, with retries.

        OpenClipboard fails while another process holds it open; a single
        try meant a whole dictated sentence vanished, so retry briefly.

        Write paths (`EmptyClipboard`/`SetClipboardData` ahead) open ONLY
        with our own hidden owner window -- never the foreground app's
        window (which would make that process the clipboard owner) and
        never NULL. If our window could not be created, a write fails
        closed. Reads prefer our window too and only then fall back to the
        historical foreground/NULL owners, which are harmless for reads.
        """
        owner = _clipboard_owner_window()
        candidates: List[object] = []
        if owner:
            candidates.append(owner)
            if not for_write:
                candidates.append(None)
        elif for_write:
            log.warning("clipboard write refused: no owner window of our own")
            return False
        else:
            candidates.extend([user32.GetForegroundWindow(), None])
        for i in range(attempts):
            for hwnd in candidates:
                if user32.OpenClipboard(hwnd):
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
                # Bounded read: `c_wchar_p(ptr).value` scanned to the first
                # NUL however far away it was, so a malformed or hostile
                # clipboard buffer could run the read off the allocation.
                # The allocation's own size bounds it, capped at 1 MiB of
                # UTF-16; a buffer with no terminator inside that bound is
                # refused (callers already treat None as "unreadable").
                size_bytes = int(kernel32.GlobalSize(handle))
                if size_bytes <= 0:
                    return None
                max_chars = min(size_bytes // 2, _MAX_CLIPBOARD_CHARS)
                if max_chars <= 0:
                    return None
                value = ctypes.wstring_at(ptr, max_chars)
                if "\0" in value:
                    return value.split("\0", 1)[0]
                if len(value) < max_chars:
                    return value  # the read itself stopped at a terminator
                log.warning("clipboard text has no terminator within its bounds; refusing it")
                return None
            finally:
                kernel32.GlobalUnlock(handle)
        except Exception as exc:  # noqa: BLE001 - fail safe: unreadable clipboard
            log.debug("clipboard read failed (%s)", type(exc).__name__)
            return None
        finally:
            user32.CloseClipboard()

    def _set_clipboard_text(self, text: str, expected_sequence: Optional[int] = None) -> bool:
        # None means this attempt has not mutated the clipboard. Never reuse
        # ownership from a prior write if allocation/open/sequence checks fail.
        self._last_written_sequence = None
        text_bytes = (text + "\0").encode("utf-16-le")
        size = len(text_bytes)

        if not self._open_clipboard():
            return False

        h_mem = None
        try:
            if expected_sequence is not None and user32.GetClipboardSequenceNumber() != expected_sequence:
                return False
            h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not h_mem:
                return False

            p_mem = kernel32.GlobalLock(h_mem)
            if not p_mem:
                return False

            ctypes.memmove(p_mem, text_bytes, size)
            kernel32.GlobalUnlock(h_mem)

            # Prepare memory before the destructive operation. Even a failed
            # SetClipboardData owns the sequence created by EmptyClipboard.
            if not user32.EmptyClipboard():
                return False
            self._last_written_sequence = user32.GetClipboardSequenceNumber()

            # On success the system takes ownership of h_mem; we must not
            # free it. On failure we still own it and must release it
            # ourselves.
            if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
                return False

            h_mem = None  # ownership transferred
            self._last_written_sequence = user32.GetClipboardSequenceNumber()
            return True
        finally:
            if h_mem:
                kernel32.GlobalFree(h_mem)
            user32.CloseClipboard()

    def _plain_clipboard(self) -> bool:
        if not self._open_clipboard():
            return False
        try:
            fmt = 0
            while True:
                fmt = user32.EnumClipboardFormats(fmt)
                if not fmt:
                    return True
                # Windows synthesizes ANSI/OEM text and locale alongside Unicode.
                if fmt not in (1, 7, 13, 16):
                    return False
        finally:
            user32.CloseClipboard()

    def paste_text(self, text: str, restore_clipboard: bool, settle_seconds: float) -> bool:
        target = user32.GetForegroundWindow()
        if not self._target_allowed(target):
            return False
        previous: Optional[str] = None
        before = user32.GetClipboardSequenceNumber()
        if restore_clipboard:
            # Preserve only plain Unicode text/empty clipboards. Do not destroy
            # image/file/rich-format clipboards while promising restoration.
            count = user32.CountClipboardFormats()
            if count and (not self._plain_clipboard() or not user32.IsClipboardFormatAvailable(CF_UNICODETEXT)):
                log.warning("clipboard contains non-plain-text formats; use type mode or clear it explicitly")
                return False
            previous = self.get_clipboard_text() if count else None
            if count and previous is None:
                return False
        owned_sequence = None
        paste_sent = False
        clipboard_changed_after_paste = False
        try:
            written = self._set_clipboard_text(text, expected_sequence=before)
            owned_sequence = self._last_written_sequence
            if not written:
                return False
            if self.get_clipboard_text() != text:
                log.warning("clipboard verification failed; aborting paste")
                return False
            if target != user32.GetForegroundWindow():
                log.warning("focused window changed; paste cancelled")
                return False
            ok = self._send_paste_keystroke()
            paste_sent = ok
            # Close the verify->paste race: the content check above ran
            # BEFORE Ctrl+V, so a clipboard manager (or the user) copying in
            # between could still make the target paste the wrong data.
            # Re-check the sequence of our successful write immediately
            # after the keystroke, before any restore/clear below.
            if (
                owned_sequence is not None
                and user32.GetClipboardSequenceNumber() != owned_sequence
            ):
                clipboard_changed_after_paste = True
                log.warning(
                    "clipboard changed after the paste keystroke; the paste "
                    "is unverifiable and the newer copy is left alone"
                )
            if settle_seconds:
                time.sleep(settle_seconds)
            return ok
        finally:
            if clipboard_changed_after_paste:
                pass  # the newer copy wins: never restore/clear over it
            elif restore_clipboard and owned_sequence is not None:
                try:
                    if previous is not None:
                        if not self._set_clipboard_text(previous, expected_sequence=owned_sequence):
                            log.warning("clipboard restore failed (WriteFailure)")
                    elif self._open_clipboard(for_write=True):
                        try:
                            if user32.GetClipboardSequenceNumber() == owned_sequence:
                                if not user32.EmptyClipboard():
                                    log.warning("clipboard restore failed (EmptyFailure)")
                        finally:
                            user32.CloseClipboard()
                    else:
                        log.warning("clipboard restore failed (OpenFailure)")
                except Exception as exc:
                    log.warning("clipboard restore failed (%s)", type(exc).__name__)
            elif not restore_clipboard and paste_sent and owned_sequence is not None:
                # restore_clipboard=false must not leave dictated PHI on a
                # shared clipboard forever: clear it to a fixed placeholder
                # (still sequence-guarded, so a newer user copy survives).
                try:
                    if self._set_clipboard_text(
                        CLIPBOARD_CLEARED_PLACEHOLDER, expected_sequence=owned_sequence
                    ):
                        log.info("clipboard cleared after paste (restore_clipboard disabled)")
                    else:
                        log.warning("clipboard clear after paste failed (WriteFailure)")
                except Exception as exc:
                    log.warning("clipboard clear after paste failed (%s)", type(exc).__name__)
