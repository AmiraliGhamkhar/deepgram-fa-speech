"""Shared font helper and Windows DPI awareness (GUI restyle R2).

`medical_stt.ui.fonts` is the single owner of UI font-family selection so the
control window and the overlay cannot drift, and it applies Windows DPI
awareness before any `tk.Tk()` interpreter exists. Both jobs are best-effort:
a font or DPI failure must never take the UI down.
"""
from __future__ import annotations

import pytest

from medical_stt.ui import fonts


@pytest.fixture(autouse=True)
def _reset_fonts():
    fonts.reset_cache()
    yield
    fonts.reset_cache()


def test_choose_family_prefers_the_first_available_persian_font():
    assert fonts.choose_family({"Arial", "Vazirmatn", "Tahoma"}) == "Vazirmatn"
    assert fonts.choose_family({"Segoe UI", "Arial"}) == "Segoe UI"
    assert fonts.choose_family({"Arial"}) == "Arial"


def test_choose_family_falls_back_to_the_platform_default():
    assert fonts.choose_family(set()) == fonts.DEFAULT_FAMILY
    assert fonts.choose_family({"Comic Sans MS"}) == fonts.DEFAULT_FAMILY


def test_family_resolution_survives_a_font_lookup_failure(monkeypatch):
    def boom(_root):
        raise RuntimeError("no font system")

    monkeypatch.setattr(fonts, "_available_families", boom)
    assert fonts.family(object()) == fonts.DEFAULT_FAMILY


def test_family_resolution_is_cached_per_process(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(
        fonts, "_available_families", lambda root: calls.append(root) or {"Vazirmatn"}
    )
    root = object()
    assert fonts.family(root) == "Vazirmatn"
    assert fonts.family(root) == "Vazirmatn"
    assert len(calls) == 1


def test_font_tuple_builds_on_the_shared_family():
    assert fonts.font(None, 8) == (fonts.family(None), 8)
    assert fonts.font(None, 10, "bold") == (fonts.family(None), 10, "bold")


def test_dpi_awareness_runs_the_windows_loader_once(monkeypatch):
    monkeypatch.setattr(fonts, "_SYSTEM", "windows")
    calls: list[int] = []
    assert fonts.enable_windows_dpi_awareness(loader=lambda: calls.append(1)) is True
    assert fonts.enable_windows_dpi_awareness(loader=lambda: calls.append(2)) is True
    assert calls == [1], "the loader must run at most once per process"


def test_dpi_awareness_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(fonts, "_SYSTEM", "windows")

    def boom():
        raise OSError("access denied")

    assert fonts.enable_windows_dpi_awareness(loader=boom) is False


def test_dpi_awareness_is_a_noop_off_windows(monkeypatch):
    monkeypatch.setattr(fonts, "_SYSTEM", "linux")
    calls: list[int] = []
    assert fonts.enable_windows_dpi_awareness(loader=lambda: calls.append(1)) is False
    assert calls == []
