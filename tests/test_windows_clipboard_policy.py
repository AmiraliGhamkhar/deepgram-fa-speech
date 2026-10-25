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


# -- B19(c): modifier re-press must not leave a logical key stuck ------------


def _drive_paste_keystroke(module, backend, user, shift_states):
    """Run the REAL _send_paste_keystroke with recorded inputs.

    `shift_states` is what GetAsyncKeyState(Shift) returns on successive
    calls: the sweep first, the re-check second.
    """
    sent = []

    def record(inputs):
        sent.extend(inputs)
        return True

    backend._send_inputs = record
    remaining = list(shift_states)

    def key_state(vk):
        if vk == module.VK_SHIFT and remaining:
            return remaining.pop(0)
        return 0

    user.GetAsyncKeyState.side_effect = key_state
    user.MapVirtualKeyW.return_value = 0
    assert module.WindowsBackend._send_paste_keystroke(backend) is True
    return [
        i for i in sent
        if i.union.ki.wVk == module.VK_SHIFT and not (i.union.ki.dwFlags & module.KEYEVENTF_KEYUP)
    ]


def test_a_modifier_released_during_paste_is_not_repressed(win):
    module, backend, user, _ = win
    # Down at the sweep (so it gets released for a clean Ctrl+V), up by the
    # re-check: re-pressing it would stick a logical Shift with no key held.
    re_presses = _drive_paste_keystroke(module, backend, user, [0x8000, 0])
    assert re_presses == []


def test_a_modifier_still_held_is_repressed(win):
    module, backend, user, _ = win
    re_presses = _drive_paste_keystroke(module, backend, user, [0x8000, 0x8000])
    assert len(re_presses) == 1


def test_last_written_sequence_is_initialized_before_any_paste(win):
    """Regression pin for audit N-06: the attribute backing
    clipboard-restore sequencing must exist from construction (None), not
    appear as a side effect of the first successful write."""
    _, backend, _, _ = win
    assert backend._last_written_sequence is None
