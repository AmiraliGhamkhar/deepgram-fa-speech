"""Canvas-drawn circular buttons, shared by every control in the window.

The control surface is a set of circles: the primary Start/Stop toggle, the
three settings actions, the quit mark and the two recovery-popup actions.
They all want the same behaviour -- an oval, a caption, an optional glyph, a
dashed focus ring, hover/press recolouring, a guaranteed click target and a
visible "why nothing happened" for a disabled press.

That behaviour used to live in `ControlWindow` as a factory plus a dict of
per-canvas state kept *outside* the widget it described, which meant every
repaint had to go back through the window. `CircleButton` owns that state
instead, so the window only decides *what* a click does.

`tk` is injected rather than imported so the widget can be built against a
stand-in in tests, which run without a display.
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional

from . import colors, fonts


def darken(color: str, factor: float = 0.9) -> str:
    """Darken a `#rrggbb` colour by `factor`, for hover/press states.

    Anything that is not a six-digit hex literal is returned unchanged: a
    hover effect must never be able to break the button it decorates.
    """
    if len(color) != 7 or not color.startswith("#"):
        return color
    try:
        channels = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    except ValueError:
        return color
    return "#" + "".join(
        f"{max(0, min(255, round(channel * factor))):02x}" for channel in channels
    )


class CircleButton:
    """One circular, clickable, keyboard-activatable control."""

    def __init__(
        self,
        tk_module: Any,
        parent: Any,
        diameter: int,
        command: Callable[[], None],
        *,
        text: str,
        fill: str,
        text_fill: str,
        font: Optional[tuple] = None,
        tag: str = "toggle",
        hover_fill: Optional[str] = None,
        primary: bool = False,
        glyph: str = "",
        on_disabled: Optional[Callable[[], None]] = None,
        on_blocked: Optional[Callable[[], None]] = None,
    ) -> None:
        self._tk = tk_module
        self._command = command
        self._on_disabled = on_disabled
        self._on_blocked = on_blocked
        self._fill = fill
        self._text_fill = text_fill
        self._hover_fill = hover_fill
        #: The primary toggle is the only circle whose ring reflects rec/ok.
        self._primary = primary
        self._enabled = True
        self._hovered = False
        self._pressed = False

        canvas = tk_module.Canvas(
            parent,
            width=diameter,
            height=diameter,
            bg=colors.BG,
            highlightthickness=0,
            cursor="hand2",
            takefocus=1,
        )
        self.canvas = canvas
        self.oval = canvas.create_oval(
            2, 2, diameter - 2, diameter - 2,
            fill=fill, outline=colors.BORDER, width=2, tags=(tag,),
        )
        # A glyph sits above the caption, so both stay inside the circle.
        self.label = canvas.create_text(
            diameter / 2,
            diameter * .67 if glyph else diameter / 2,
            text=text,
            fill=text_fill,
            font=font or fonts.font(parent, 12, "bold"),
            tags=(tag,),
            width=max(8, diameter - 10),
            justify="center",
        )
        self._labels: List[int] = [self.label]
        if glyph:
            self._labels.append(
                canvas.create_text(
                    diameter / 2, diameter * .28,
                    text=glyph, fill=text_fill,
                    font=fonts.font(parent, 11), tags=(tag,),
                )
            )
        self._focus = canvas.create_oval(
            1, 1, diameter - 1, diameter - 1,
            fill="", outline=colors.ACCENT, width=1, dash=(2, 2), state="hidden",
        )

        canvas.tag_bind(tag, "<Button-1>", self._press)
        canvas.tag_bind(tag, "<Enter>", lambda _event: self._hover(True))
        canvas.tag_bind(tag, "<Leave>", lambda _event: self._hover(False))
        canvas.bind("<ButtonRelease-1>", self._release)
        canvas.bind("<FocusIn>", lambda _event: canvas.itemconfig(self._focus, state="normal"))
        canvas.bind("<FocusOut>", lambda _event: canvas.itemconfig(self._focus, state="hidden"))
        canvas.bind("<Return>", self._activate)
        canvas.bind("<space>", self._activate)

    # -- widget access ----------------------------------------------------
    def widget(self) -> Any:
        return self.canvas

    def pack(self, **kwargs: Any) -> None:
        self.canvas.pack(**kwargs)

    # -- appearance -------------------------------------------------------
    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._pressed = False
        self.canvas.config(cursor="hand2" if enabled else "arrow")
        self.paint()

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def set_style(self, *, fill: Optional[str] = None, text: Optional[str] = None) -> None:
        """Recolour and/or relabel; both are applied by one repaint."""
        if fill is not None:
            self._fill = fill
        if text is not None:
            self.canvas.itemconfig(self.label, text=text)
        self.paint()

    def set_hovered(self, hovered: bool) -> None:
        self._hovered = hovered
        if not hovered:
            self._pressed = False
        self.paint()

    def paint(self) -> None:
        """Recompute the colours of the oval, ring and captions."""
        fill = self._fill
        if not self._enabled:
            fill = colors.blend(fill, colors.BG)
        elif self._pressed:
            fill = darken(fill, .8)
        elif self._hovered:
            fill = self._hover_fill or darken(fill)
        ring = colors.REC if self._primary and self._fill == colors.REC else colors.BORDER
        self.canvas.itemconfig(self.oval, fill=fill, outline=ring)
        ink = (
            self._text_fill
            if self._enabled
            else colors.blend(self._text_fill, colors.BG)
        )
        for label in self._labels:
            self.canvas.itemconfig(label, fill=ink)

    # -- interaction ------------------------------------------------------
    def _activate(self, _event: object = None) -> None:
        """A click or key press: run the action, or explain the refusal."""
        if self._enabled:
            self._command()
        elif self._on_disabled is not None:
            self._on_disabled()
        elif self._on_blocked is not None:
            self._on_blocked()

    def _press(self, _event: object) -> None:
        self.canvas.focus_set()
        self._pressed = True
        self.paint()
        self._activate()

    def _release(self, _event: object) -> None:
        self._pressed = False
        self.paint()

    def _hover(self, entered: bool) -> None:
        self.set_hovered(entered)
