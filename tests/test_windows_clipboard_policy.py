"""Exercise Win32 orchestration with fake DLLs; never inject native input.

These tests prove policy and call ordering, not real Windows ABI or timing.
"""
from __future__ import annotations

import ctypes
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def win(monkeypatch):
    import medical_stt.injection
    # Load under a separate package-qualified name so other tests retain the
    # genuine platform module and no fake DLL survives the fixture.
    user, kernel = MagicMock(), MagicMock()
    monkeypatch.setattr("platform.system", lambda: "Windows")
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: user if name == "user32" else kernel, raising=False)
    path = Path(medical_stt.injection.__file__).parent / "_windows_backend.py"
    spec = importlib.util.spec_from_file_location("medical_stt.injection._fake_native_policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend = module.WindowsBackend()
    state = {"text": "original", "seq": 1, "pastes": 0, "writes": []}
    user.GetForegroundWindow.return_value = 123

    def _foreign_pid(_hwnd, pid):
        # A live external process by default; B20 refuses pid 0, and the
        # bare MagicMock used to leave the DWORD at its 0 default.
        ctypes.cast(pid, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = 4242
        return 1

    user.GetWindowThreadProcessId.side_effect = _foreign_pid
    user.CountClipboardFormats.return_value = 1
    user.IsClipboardFormatAvailable.return_value = True
    user.GetClipboardSequenceNumber.side_effect = lambda: state["seq"]
    user.OpenClipboard.return_value = True
    backend._plain_clipboard = lambda: True
    backend.get_clipboard_text = lambda: state["text"]

    def set_text(text, expected_sequence=None):
        if expected_sequence is not None and expected_sequence != state["seq"]:
            return False
        state["seq"] += 1
        state["text"] = text
        state["writes"].append(text)
        backend._last_written_sequence = state["seq"]
        return True

    def paste():
        state["pastes"] += 1
        return True

    backend._set_clipboard_text = set_text
    backend._send_paste_keystroke = paste
    return module, backend, user, state


def test_plain_text_is_restored(win):
    _, backend, _, state = win
    assert backend.paste_text("dictation", True, 0)
    assert state["text"] == "original"
    assert state["pastes"] == 1


def test_rich_clipboard_is_not_destroyed(win):
    _, backend, _, state = win
    backend._plain_clipboard = lambda: False
    assert not backend.paste_text("dictation", True, 0)
    assert not state["writes"]
    assert not state["pastes"]


def test_copy_during_paste_is_not_overwritten(win):
    _, backend, _, state = win
    def paste():
        state["text"] = "new user copy"
        state["seq"] += 1
        return True
    backend._send_paste_keystroke = paste
    assert backend.paste_text("dictation", True, 0)
    assert state["text"] == "new user copy"
    assert state["writes"] == ["dictation"]


def test_focus_change_cancels_paste_and_restores_clipboard(win):
    _, backend, user, state = win
    user.GetForegroundWindow.side_effect = [123, 456]
    assert not backend.paste_text("dictation", True, 0)
    assert state["pastes"] == 0
    assert state["text"] == "original"


def test_copy_between_set_and_verification_does_not_restore_stale_text(win):
    _, backend, _, state = win
    def read():
        if state["text"] == "dictation":
            state["seq"] += 1
            state["text"] = "new user copy"
        return state["text"]
    backend.get_clipboard_text = read
    assert not backend.paste_text("dictation", True, 0)
    assert state["text"] == "new user copy"
    assert not state["pastes"]


def test_restore_sequence_checked_while_clipboard_is_open(win):
    module, backend, user, _ = win
    # Restore the real setter: a stale sequence must return before EmptyClipboard.
    assert not module.WindowsBackend._set_clipboard_text(backend, "old", expected_sequence=999)
    user.OpenClipboard.assert_called()
    user.EmptyClipboard.assert_not_called()
    user.CloseClipboard.assert_called()


def test_empty_clipboard_is_cleared_after_paste(win):
    _, backend, user, state = win
    user.CountClipboardFormats.return_value = 0
    assert backend.paste_text("dictation", True, 0)
    assert state["pastes"] == 1
    user.EmptyClipboard.assert_called_once()


def test_production_refuses_its_own_settings_window(win):
    import os
    _, backend, user, state = win
    def get_pid(hwnd, pid):
        ctypes.cast(pid, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = os.getpid()
        return 1
    user.GetWindowThreadProcessId.side_effect = get_pid
    assert not backend.paste_text("dictation", True, 0)
    assert not state["writes"]
    assert not state["pastes"]


def test_diagnostic_expected_target_cannot_be_replaced_by_another_app(win):
    _, backend, user, state = win
    backend._expected_target = 123
    user.GetForegroundWindow.return_value = 456
    assert not backend.paste_text("synthetic text", True, 0)
    assert not state["writes"]
    assert not state["pastes"]


# -- modifier re-press must not leave a logical key stuck --------------------


@pytest.mark.parametrize("modifier", ["VK_SHIFT", "VK_MENU", "VK_LWIN", "VK_RWIN", "VK_CONTROL"])
def test_modifier_released_during_paste_is_not_repressed(win, modifier):
    """The pre-paste snapshot must not be re-pressed verbatim: a modifier
    the user physically released during the paste was left logically stuck,
    turning the user's next keystrokes into shortcuts."""
    module, backend, user, _ = win
    vk = getattr(module, modifier)
    physical_down = {vk}
    sent = []

    def record(inputs):
        for inp in inputs:
            key = inp.union.ki.wVk
            key_up = bool(inp.union.ki.dwFlags & module.KEYEVENTF_KEYUP)
            sent.append((key, key_up))
            if key == module.VK_V and not key_up:
                physical_down.discard(vk)  # the user lets go mid-paste
        return True

    backend._send_inputs = record
    user.GetAsyncKeyState.side_effect = lambda key: 0x8000 if key in physical_down else 0
    user.MapVirtualKeyW.return_value = 0
    assert module.WindowsBackend._send_paste_keystroke(backend)
    assert sent[0] == (vk, True), "the modifier is still released before the paste"
    assert sent[-1] != (vk, False), "a released modifier must not be re-pressed"


@pytest.mark.parametrize("modifier", ["VK_SHIFT", "VK_MENU", "VK_LWIN", "VK_RWIN", "VK_CONTROL"])
def test_modifier_still_held_after_paste_is_repressed(win, modifier):
    """A modifier still reported down by a FRESH async-state read after the
    paste (both in the snapshot and still held) is re-pressed as before."""
    module, backend, user, _ = win
    vk = getattr(module, modifier)
    physical_down = {vk}
    sent = []

    def record(inputs):
        for inp in inputs:
            sent.append((inp.union.ki.wVk, bool(inp.union.ki.dwFlags & module.KEYEVENTF_KEYUP)))
        return True

    backend._send_inputs = record
    user.GetAsyncKeyState.side_effect = lambda key: 0x8000 if key in physical_down else 0
    user.MapVirtualKeyW.return_value = 0
    assert module.WindowsBackend._send_paste_keystroke(backend)
    assert sent[0] == (vk, True)
    assert sent[-1] == (vk, False)


def test_a_target_with_pid_zero_is_refused(win):
    """B20: a just-destroyed foreground window yields pid 0 (the idle
    process), which `_target_allowed` used to accept."""
    _, backend, user, state = win

    def get_pid(hwnd, pid):
        ctypes.cast(pid, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = 0
        return 1  # a thread id, but no owning process

    user.GetWindowThreadProcessId.side_effect = get_pid
    assert not backend.paste_text("dictation", True, 0)
    assert not state["writes"]
    assert not state["pastes"]


def test_last_written_sequence_is_initialized_before_any_paste(win):
    """Regression pin for audit N-06: the attribute backing
    clipboard-restore sequencing must exist from construction (None), not
    appear as a side effect of the first successful write."""
    _, backend, _, _ = win
    assert backend._last_written_sequence is None


@pytest.mark.parametrize("failure", ["allocate", "lock", "set"])
def test_failed_native_clipboard_write_preserves_original(win, failure):
    module, backend, user, state = win
    kernel = module.kernel32
    buffer = ctypes.create_string_buffer(1024)
    attempts = {"allocate": 0, "lock": 0, "set": 0}

    def allocate(*args):
        attempts["allocate"] += 1
        return 0 if failure == "allocate" and attempts["allocate"] == 1 else 123

    def lock(*args):
        attempts["lock"] += 1
        return 0 if failure == "lock" and attempts["lock"] == 1 else ctypes.addressof(buffer)

    def empty():
        state["text"] = ""
        state["seq"] += 1
        return True

    def set_data(*args):
        attempts["set"] += 1
        if failure == "set" and attempts["set"] == 1:
            return 0
        state["text"] = buffer.raw.decode("utf-16-le").split("\0", 1)[0]
        state["seq"] += 1
        return 123

    kernel.GlobalAlloc.side_effect = allocate
    kernel.GlobalLock.side_effect = lock
    user.EmptyClipboard.side_effect = empty
    user.SetClipboardData.side_effect = set_data
    backend._set_clipboard_text = module.WindowsBackend._set_clipboard_text.__get__(backend)
    backend._last_written_sequence = 0  # stale prior ownership must not block recovery
    assert not backend.paste_text("clinical text", True, 0)
    assert state["text"] == "original"
    assert not state["pastes"]
    if failure in ("allocate", "lock"):
        user.EmptyClipboard.assert_not_called()


def test_clipboard_restore_failure_is_logged_without_content(win, caplog):
    _, backend, _, _ = win
    write = backend._set_clipboard_text
    def fail_restore(text, expected_sequence=None):
        if text == "original":
            return False
        return write(text, expected_sequence)
    backend._set_clipboard_text = fail_restore
    assert backend.paste_text("clinical text", True, 0)
    assert "restore" in caplog.text
    assert "clinical text" not in caplog.text
    assert "original" not in caplog.text


# -- clipboard ownership: our own hidden window, never the foreground app ---


def test_write_path_opens_clipboard_with_our_own_window(win):
    module, backend, user, _ = win
    user.CreateWindowExW.return_value = 777
    opened = []
    user.OpenClipboard.side_effect = lambda hwnd: opened.append(hwnd) or True
    assert module.WindowsBackend._open_clipboard(backend, for_write=True)
    assert opened == [777], "writes must own the clipboard via our window, not the foreground app (123) or NULL"


def test_write_never_falls_back_to_null_or_foreground_owner(win):
    module, backend, user, _ = win
    user.CreateWindowExW.return_value = 0  # our window could not be created
    opened = []
    user.OpenClipboard.side_effect = lambda hwnd: opened.append(hwnd) or True
    assert not module.WindowsBackend._open_clipboard(backend, for_write=True)
    assert opened == [], "a write with no owner window of our own must fail, not borrow one"
    # Reads may keep the old fallbacks, but still only after our window failed.
    assert module.WindowsBackend._open_clipboard(backend)
    assert opened and opened[0] is not None


def test_read_path_prefers_our_own_window(win):
    module, backend, user, _ = win
    user.CreateWindowExW.return_value = 777
    opened = []
    user.OpenClipboard.side_effect = lambda hwnd: opened.append(hwnd) or True
    assert module.WindowsBackend._open_clipboard(backend)
    assert opened == [777]


# -- verify -> paste race -----------------------------------------------------


def test_clipboard_change_after_paste_keystroke_is_unverifiable(win, caplog):
    import logging

    _, backend, _, state = win

    def paste():
        state["text"] = "new user copy"
        state["seq"] += 1
        return True

    backend._send_paste_keystroke = paste
    with caplog.at_level(logging.WARNING, logger="medical_stt.injection.windows"):
        assert backend.paste_text("dictation", True, 0)
    assert state["text"] == "new user copy", "the newer copy wins; nothing is restored over it"
    assert state["writes"] == ["dictation"]
    assert "unverifiable" in caplog.text


# -- restore_clipboard: false must not retain PHI ------------------------------


def test_no_restore_replaces_dictated_text_with_placeholder(win, caplog):
    import logging

    from medical_stt.injection.backend import CLIPBOARD_CLEARED_PLACEHOLDER

    _, backend, _, state = win
    with caplog.at_level(logging.INFO, logger="medical_stt.injection.windows"):
        assert backend.paste_text("dictation", False, 0)
    assert state["text"] == CLIPBOARD_CLEARED_PLACEHOLDER
    assert state["writes"][-1] == CLIPBOARD_CLEARED_PLACEHOLDER
    assert "cleared" in caplog.text
    assert "dictation" not in caplog.text


def test_failed_set_after_empty_clipboard_still_records_sequence(win):
    """Even when SetClipboardData fails, EmptyClipboard already created a
    new sequence; recording it is what lets paste_text's finally restore."""
    module, backend, user, state = win
    kernel = module.kernel32
    buffer = ctypes.create_string_buffer(1024)
    kernel.GlobalAlloc.return_value = 123
    kernel.GlobalLock.return_value = ctypes.addressof(buffer)

    def empty():
        state["seq"] += 1
        return True

    user.EmptyClipboard.side_effect = empty
    user.SetClipboardData.return_value = 0
    real_set = module.WindowsBackend._set_clipboard_text.__get__(backend)
    assert not real_set("clinical text")
    assert backend._last_written_sequence == state["seq"]


# -- bounded clipboard read -----------------------------------------------------


def test_clipboard_read_is_bounded_and_requires_a_terminator(win, monkeypatch):
    module, backend, user, _ = win
    kernel = module.kernel32

    # Linux c_wchar is 4 bytes, so decode the UTF-16LE buffer explicitly;
    # what is under test is the bound the code passes and its NUL check.
    def fake_wstring_at(ptr, count):
        return ctypes.string_at(ptr, count * 2).decode("utf-16-le")

    monkeypatch.setattr(ctypes, "wstring_at", fake_wstring_at)
    user.GetClipboardData.return_value = 999
    kernel.GlobalUnlock.return_value = True

    raw = "abc\0".encode("utf-16-le")
    buffer = ctypes.create_string_buffer(raw, len(raw))
    kernel.GlobalLock.return_value = ctypes.addressof(buffer)
    kernel.GlobalSize.return_value = len(raw)
    assert module.WindowsBackend.get_clipboard_text(backend) == "abc"

    raw = "abcd".encode("utf-16-le")  # no terminator inside the bound
    buffer = ctypes.create_string_buffer(raw, len(raw))
    kernel.GlobalLock.return_value = ctypes.addressof(buffer)
    kernel.GlobalSize.return_value = len(raw)
    assert module.WindowsBackend.get_clipboard_text(backend) is None

    raw = "abcdef\0".encode("utf-16-le")  # terminator beyond the cap
    buffer = ctypes.create_string_buffer(raw, len(raw))
    kernel.GlobalLock.return_value = ctypes.addressof(buffer)
    kernel.GlobalSize.return_value = len(raw)
    monkeypatch.setattr(module, "_MAX_CLIPBOARD_CHARS", 2)
    assert module.WindowsBackend.get_clipboard_text(backend) is None


# -- NULL foreground target ------------------------------------------------------


def test_null_foreground_gets_the_focus_warning(win, caplog):
    import logging

    _, backend, user, _ = win
    user.GetWindowThreadProcessId.reset_mock()
    user.GetWindowThreadProcessId.side_effect = lambda *_a: 0
    with caplog.at_level(logging.WARNING, logger="medical_stt.injection.windows"):
        assert not backend._target_allowed(0)
    assert "focus an external document" in caplog.text
    user.GetWindowThreadProcessId.assert_not_called()


# -- type path modifier hygiene ---------------------------------------------------


def test_unicode_typing_releases_and_restores_interfering_modifiers(win):
    module, backend, user, _ = win
    physical_down = {module.VK_MENU}
    batches = []

    def record(inputs):
        batches.append([(i.union.ki.wVk, bool(i.union.ki.dwFlags & module.KEYEVENTF_KEYUP)) for i in inputs])
        return True

    backend._send_inputs = record
    user.GetAsyncKeyState.side_effect = lambda key: 0x8000 if key in physical_down else 0
    user.MapVirtualKeyW.return_value = 0
    assert module.WindowsBackend.send_unicode_text(backend, "A")
    assert batches[0] == [(module.VK_MENU, True)], "held Alt must be released before typing"
    assert batches[-1] == [(module.VK_MENU, False)], "still-held Alt is restored afterwards"
