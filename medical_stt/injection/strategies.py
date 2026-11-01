"""How a finalized utterance is delivered to the focused application.

Two delivery modes exist and they differ only in *how* text reaches the
target: synthetic typing (per-character key events) or a clipboard paste.
Keeping them behind one interface means the session layer never branches on
`settings.inject_mode`, and a third mode is a new class rather than another
`if` in the middle of the transcription path.
"""
from __future__ import annotations

from typing import Protocol

from .text_injector import TextInjector

#: Inject modes accepted by configuration validation.
TYPE_MODE = "type"
PASTE_MODE = "paste"


class InjectionStrategy(Protocol):
    """Delivers one finalized utterance."""

    mode: str

    def deliver(self, injector: TextInjector, text: str) -> bool: ...


class TypeInjection:
    """Type the utterance character by character.

    A trailing separator is added unless the text already ends in whitespace:
    consecutive dictations must not fuse into one token
    ("فشار خون120/80بیمار" instead of "فشار خون 120/80 بیمار"). After a
    newline-terminated utterance an unconditional space would start the next
    line with a stray leading space, hence the check.
    """

    mode = TYPE_MODE

    def deliver(self, injector: TextInjector, text: str) -> bool:
        separator = "" if text[-1:].isspace() else " "
        return injector.type_text(text + separator, add_directional_mark=True)


class PasteInjection:
    """Paste the utterance through the clipboard.

    `TextInjector.paste_text` recovers the word separator itself (see
    `_trailing_separator`), so the separator survives whitespace normalization
    and is dropped after a line break.
    """

    mode = PASTE_MODE

    def deliver(self, injector: TextInjector, text: str) -> bool:
        return injector.paste_text(text + " ", add_rtl_mark=True)


_STRATEGIES: dict[str, type[InjectionStrategy]] = {
    TYPE_MODE: TypeInjection,
    PASTE_MODE: PasteInjection,
}


def strategy_for(mode: str) -> InjectionStrategy:
    """Return the strategy for a configured inject mode.

    Unknown modes fall back to paste, which is the default and the mode that
    cannot lose text between keystrokes.
    """
    factory = _STRATEGIES.get(mode, PasteInjection)
    return factory()
