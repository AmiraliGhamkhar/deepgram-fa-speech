"""The deterministic text pipeline applied to every final utterance.

The pipeline is a sequence of small stages behind one interface, so the
session code never grows a new ``if`` when a stage is added, reordered or
disabled:

    normalize -> confidence guard -> terminology -> (BiDi at the injection
    boundary)

Each stage receives a mutable `UtteranceContext` and returns it; a stage that
has nothing to do is a no-op. **Determinism is the point**: there is no model,
no scoring and no network call anywhere in this chain, and a low-confidence
clinical token can only become *more* protected, never more authoritative.

BiDi formatting is deliberately *not* a stage: directional marks belong to the
injection boundary (`medical_stt/injection/`), so the text logged, retained for
recovery and shown in the overlay is the same logical text the clinician spoke.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Sequence, Tuple

from ..stt.base import TranscriptEvent, WordInfo
from .confidence import (
    MedicalConfidenceWarning,
    find_low_confidence_medical_words,
    low_confidence_protected_ranges,
)
from .normalize import normalize
from .terminology import TerminologyEngine

log = logging.getLogger("medical_stt.app")

ProtectedRange = Tuple[int, int]


@dataclass
class UtteranceContext:
    """One utterance travelling through the pipeline."""

    text: str
    words: Tuple[WordInfo, ...] = ()
    normalized: str = ""
    #: Set by the confidence stage.
    confidence_warning: Optional[MedicalConfidenceWarning] = None
    #: Ranges a later stage must not rewrite. `None` means "not computed";
    #: `protect_everything` means alignment failed and nothing may be rewritten.
    protected_ranges: Optional[List[ProtectedRange]] = None
    protect_everything: bool = False
    #: Wall-clock cost per stage, in execution order.
    timings: dict = field(default_factory=dict)


class TextStage(Protocol):
    """A single deterministic transformation of an utterance."""

    name: str

    def apply(self, context: UtteranceContext) -> None: ...


@dataclass(frozen=True)
class PipelineOutcome:
    """What the session layer needs from a processed utterance."""

    text: str
    normalized: str
    confidence_warning: Optional[MedicalConfidenceWarning]
    timings: dict


class NormalizationStage:
    """Unicode/digit/whitespace normalization (see `processing/normalize.py`)."""

    name = "normalize"

    def apply(self, context: UtteranceContext) -> None:
        context.text = normalize(context.text)
        context.normalized = context.text


class ConfidenceGuardStage:
    """Find low-confidence clinical tokens and protect their spans.

    A low-confidence clinical token must not be transformed into a more
    authoritative-looking concept, so only its word/timing-derived span is
    protected -- reviewed corrections elsewhere in the utterance still fire.
    If alignment cannot establish that span, the stage fails safe and protects
    the whole utterance.
    """

    name = "confidence"

    def __init__(self, threshold: float) -> None:
        self._threshold = threshold

    def apply(self, context: UtteranceContext) -> None:
        warning = find_low_confidence_medical_words(context.words, self._threshold)
        context.confidence_warning = warning
        if warning is None:
            context.protected_ranges = []
            return
        categories = {item.category for item in warning.uncertain_words}
        log.warning(
            "low-confidence medical tokens: count=%d categories=%s (uncertain spans protected)",
            len(warning.uncertain_words),
            ",".join(sorted(categories)),
        )
        spans = low_confidence_protected_ranges(context.text, context.words, warning)
        if spans is None:
            log.warning("could not map low-confidence token spans; preserving the whole utterance")
            context.protect_everything = True
            context.protected_ranges = None
            return
        context.protected_ranges = list(spans)


class TerminologyStage:
    """Apply the reviewed terminology rules, honouring protected spans."""

    name = "terminology"

    def __init__(self, engine: TerminologyEngine) -> None:
        self._engine = engine

    def apply(self, context: UtteranceContext) -> None:
        if context.protect_everything:
            return
        context.text = self._engine.apply(
            context.text, protected_ranges=context.protected_ranges or []
        )


class TranscriptPipeline:
    """Runs the stages in order and reports their cost."""

    def __init__(self, stages: Sequence[TextStage]) -> None:
        self._stages = list(stages)

    @property
    def stage_names(self) -> Tuple[str, ...]:
        return tuple(stage.name for stage in self._stages)

    def run(self, event: TranscriptEvent) -> PipelineOutcome:
        context = UtteranceContext(text=event.text, words=tuple(event.words))
        for stage in self._stages:
            started = time.monotonic()
            stage.apply(context)
            context.timings[stage.name] = time.monotonic() - started
        return PipelineOutcome(
            text=context.text,
            normalized=context.normalized or context.text,
            confidence_warning=context.confidence_warning,
            timings=context.timings,
        )

    @classmethod
    def default(cls, engine: TerminologyEngine, threshold: float) -> "TranscriptPipeline":
        """The production pipeline: normalize -> guard -> terminology."""
        return cls([NormalizationStage(), ConfidenceGuardStage(threshold), TerminologyStage(engine)])
