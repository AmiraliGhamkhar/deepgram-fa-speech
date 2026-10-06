"""Minimal in-process metrics for the host service.

Deliberately dependency-free: this deployment does not need a Prometheus
server, a statsd agent, or an OpenTelemetry collector, and adding one would
be more moving parts than the service itself. What it does need is a way to
answer operational questions during an incident about *token issuance*,
which is the only thing this service does:

    session_starts_total, session_success_total, session_failures_total,
    token_requests_total, token_request_failures_total,
    token_request_latency, deepgram_connection_failures_total,
    auth_failures_total, metrics_auth_failures_total, rate_limited_total,
    request_body_rejections_total, inflight_grants, inflight_grants_capacity,
    registry_clients

so this module implements counters, gauges and latency histograms and
renders them in the Prometheus text exposition format (a plain string, so an
operator can curl the endpoint or point a real Prometheus at it later with
no code change).

**Only series this process can actually observe are declared.** Per-session
audio and transcript timings (`active_sessions`, `audio_queue_depth`,
`session_duration`, `first_interim_latency`, ...) live on the desktop
client: the host mints a token and never sees the stream, so publishing them
here would emit a permanent, plausible-looking zero that a dashboard would
read as "no sessions active" during a fully busy clinic. They are
deliberately absent -- see `LiveMedicalSTT.session_health` for the
client-side equivalent.

**Privacy invariant (non-negotiable):** this module stores counters and
durations only. It must never be given a token, a secret, a client
credential, or transcript text. Client ids are accepted as labels because
they are identifiers, not credentials, and `MAX_LABELLED_CLIENTS` bounds how
many distinct ids can be tracked so label cardinality cannot grow without
limit.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Dict, Iterable, List, Tuple

#: Above this many distinct client ids, further ids collapse into an
#: ``other`` bucket instead of creating an unbounded number of time series.
MAX_LABELLED_CLIENTS = 2_000

#: Latency buckets, in seconds. The 2.0 boundary lines up with the
#: acceptance target for token acquisition (p95 < 2s), so a scrape can tell
#: "inside target" from "outside target" without adding buckets.
LATENCY_BUCKETS: Tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0,
)

_OVERFLOW_LABEL = "other"


def _escape_label_value(value: str) -> str:
    """Escape a label value per the Prometheus text format."""
    return (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    )


class _Histogram:
    """Cumulative latency histogram with a fixed, bounded bucket set."""

    def __init__(self, buckets: Iterable[float] = LATENCY_BUCKETS) -> None:
        self._buckets = tuple(sorted(float(b) for b in buckets))
        self._counts = [0] * len(self._buckets)
        self._sum = 0.0
        self._count = 0

    def observe(self, value: float) -> None:
        self._sum += float(value)
        self._count += 1
        for index, upper in enumerate(self._buckets):
            if value <= upper:
                self._counts[index] += 1

    def snapshot(self) -> Tuple[Tuple[Tuple[float, int], ...], float, int]:
        return tuple(zip(self._buckets, self._counts, strict=True)), self._sum, self._count


class Metrics:
    """Thread-safe counters, gauges and histograms with bounded cardinality."""

    def __init__(self, max_labelled_clients: int = MAX_LABELLED_CLIENTS) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, Dict[Tuple[Tuple[str, str], ...], int]] = {}
        self._gauges: Dict[str, Dict[Tuple[Tuple[str, str], ...], float]] = {}
        self._histograms: Dict[str, Dict[Tuple[Tuple[str, str], ...], _Histogram]] = {}
        self._max_labelled_clients = max(1, max_labelled_clients)
        self._tracked_clients: set = set()
        self._started_at = time.time()

    # -- recording ------------------------------------------------------

    def increment(self, name: str, value: int = 1, **labels: str) -> None:
        if not self._accept_labels(labels):
            return
        key = self._label_key(labels)
        with self._lock:
            series = self._counters.setdefault(name, {})
            series[key] = series.get(key, 0) + value

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        if not self._accept_labels(labels):
            return
        key = self._label_key(labels)
        with self._lock:
            self._gauges.setdefault(name, {})[key] = float(value)

    def observe(self, name: str, value: float, **labels: str) -> None:
        if not self._accept_labels(labels):
            return
        key = self._label_key(labels)
        with self._lock:
            series = self._histograms.setdefault(name, {})
            histogram = series.get(key)
            if histogram is None:
                histogram = _Histogram()
                series[key] = histogram
            histogram.observe(value)

    def _accept_labels(self, labels: Dict[str, str]) -> bool:
        """Bound the per-client label cardinality, in place.

        A client id that is already tracked passes. A new one is admitted
        until the cap, after which it is rewritten to ``other`` -- so a flood
        of fabricated ids cannot create unbounded time series.

        Rewritten, not dropped: discarding the sample would make the counters
        *under-report* exactly when the host is under the most abuse, which is
        when they are being read. Folding keeps the total correct and costs
        one extra series.
        """
        client_id = labels.get("client_id")
        if not client_id:
            return True
        with self._lock:
            if client_id in self._tracked_clients:
                return True
            if len(self._tracked_clients) >= self._max_labelled_clients:
                labels["client_id"] = _OVERFLOW_LABEL
                return True
            self._tracked_clients.add(client_id)
            return True

    @staticmethod
    def _label_key(labels: Dict[str, str]) -> Tuple[Tuple[str, str], ...]:
        return tuple(sorted((str(k), str(v)) for k, v in labels.items() if v is not None))

    # -- reading --------------------------------------------------------

    def counter_value(self, name: str, **labels: str) -> int:
        with self._lock:
            return self._counters.get(name, {}).get(self._label_key(labels), 0)

    def gauge_value(self, name: str, **labels: str) -> float:
        with self._lock:
            return self._gauges.get(name, {}).get(self._label_key(labels), 0.0)

    @property
    def tracked_client_labels(self) -> int:
        with self._lock:
            return len(self._tracked_clients)

    @property
    def uptime_seconds(self) -> float:
        return max(0.0, time.time() - self._started_at)

    def quantiles(self, name: str, quantiles: Iterable[float] = (0.5, 0.95, 0.99)) -> Dict[str, float]:
        """Approximate quantiles from the bucket boundaries.

        Bucket counts are *cumulative* (that is what the Prometheus text
        format requires, and what `_Histogram.observe` records), so the
        estimate is the first boundary whose cumulative count reaches
        `q * count`. Re-accumulating them -- summing an already cumulative
        series -- silently reports a far smaller latency than was measured:
        for observations {0.001, 0.02, 0.3, 1.5, 40} it returned p95=0.05s
        instead of 30s, which would let a capacity gate pass a system that
        is missing its target by an order of magnitude.

        Resolution is the bucket boundary, not an interpolation inside it:
        the point is a stable, comparable number. An observation above the
        largest boundary is reported *as* the largest boundary, i.e. the
        estimate never claims to be faster than the evidence allows.
        """
        wanted = sorted(float(q) for q in quantiles)
        with self._lock:
            series = self._histograms.get(name, {})
            if not series:
                return {f"p{int(q * 100)}": math.nan for q in wanted}
            merged = _Histogram()
            for histogram in series.values():
                buckets, total, count = histogram.snapshot()
                # Summing cumulative counts across label sets gives the
                # cumulative counts of the union.
                for index, (_upper, hits) in enumerate(buckets):
                    merged._counts[index] += hits
                merged._sum += total
                merged._count += count
            buckets, _total_sum, count = merged.snapshot()

        if count == 0:
            return {f"p{int(q * 100)}": math.nan for q in wanted}

        result: Dict[str, float] = {}
        for q in wanted:
            target = q * count
            estimate = buckets[-1][0] if buckets else 0.0
            for upper, cumulative_hits in buckets:
                if cumulative_hits >= target:
                    estimate = upper
                    break
            result[f"p{int(q * 100)}"] = round(estimate, 6)
        return result

    # -- rendering ------------------------------------------------------

    def render(self) -> str:
        """Render the Prometheus text exposition format."""
        lines: List[str] = []
        with self._lock:
            counters = {name: dict(series) for name, series in self._counters.items()}
            gauges = {name: dict(series) for name, series in self._gauges.items()}
            histograms = {
                name: {key: hist.snapshot() for key, hist in series.items()}
                for name, series in self._histograms.items()
            }

        for name in sorted(counters):
            lines.append(f"# TYPE {name} counter")
            for key, count_value in sorted(counters[name].items()):
                lines.append(f"{name}{_format_labels(key)} {count_value}")

        for name in sorted(gauges):
            lines.append(f"# TYPE {name} gauge")
            for key, gauge_value in sorted(gauges[name].items()):
                lines.append(f"{name}{_format_labels(key)} {_as_number(gauge_value)}")

        for name in sorted(histograms):
            lines.append(f"# TYPE {name} histogram")
            for key, (buckets, histogram_sum, count) in sorted(histograms[name].items()):
                base = _format_labels(key)
                for upper, hits in buckets:
                    label_text = _merge_labels(key, [("le", _as_number(upper))])
                    lines.append(f"{name}_bucket{label_text} {hits}")
                lines.append(f"{name}_bucket{_merge_labels(key, [('le', '+Inf')])} {count}")
                lines.append(f"{name}_sum{base} {_as_number(histogram_sum)}")
                lines.append(f"{name}_count{base} {count}")

        lines.append(f"host_uptime_seconds {_as_number(self.uptime_seconds)}")
        return "\n".join(lines) + "\n"


def _format_labels(key: Tuple[Tuple[str, str], ...]) -> str:
    if not key:
        return ""
    body = ",".join(f'{name}="{_escape_label_value(value)}"' for name, value in key)
    return "{" + body + "}"


def _merge_labels(
    key: Tuple[Tuple[str, str], ...], extra: List[Tuple[str, str]]
) -> str:
    merged = dict(key)
    for name, value in extra:
        merged[name] = value
    return _format_labels(tuple(sorted(merged.items())))


def _as_number(value: float) -> str:
    """Format a float without an exponent, which Prometheus text rejects."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


#: Names the host always publishes, so `/metrics` is stable even before the
#: first request arrives and a dashboard does not show gaps. Every one of
#: these is a series this process actually updates -- see the module
#: docstring for why client-side series are deliberately not declared here.
DECLARED_COUNTERS: Tuple[str, ...] = (
    "session_starts_total",
    "session_success_total",
    "session_failures_total",
    "token_requests_total",
    "token_request_failures_total",
    "deepgram_connection_failures_total",
    "auth_failures_total",
    "metrics_auth_failures_total",
    "rate_limited_total",
    "request_body_rejections_total",
)

DECLARED_GAUGES: Tuple[str, ...] = (
    #: Deepgram grant requests currently in flight, and the cap they are
    #: shed against (`HOST_MAX_INFLIGHT_GRANTS`).
    "inflight_grants",
    "inflight_grants_capacity",
    #: Client identities the host can authenticate.
    "registry_clients",
)

DECLARED_HISTOGRAMS: Tuple[str, ...] = (
    "token_request_latency",
)


def new_metrics(max_labelled_clients: int = MAX_LABELLED_CLIENTS) -> Metrics:
    """A registry pre-seeded with the declared counters and gauges at zero.

    Histograms are not pre-seeded: an empty `token_request_latency` renders
    no series at all, whereas pre-seeding one would publish `_count 0` with
    no buckets, which Prometheus rejects as an inconsistent histogram.
    """
    metrics = Metrics(max_labelled_clients=max_labelled_clients)
    for name in DECLARED_COUNTERS:
        metrics.increment(name, 0)
    for name in DECLARED_GAUGES:
        metrics.set_gauge(name, 0)
    return metrics


__all__ = [
    "Metrics",
    "new_metrics",
    "LATENCY_BUCKETS",
    "MAX_LABELLED_CLIENTS",
    "DECLARED_COUNTERS",
    "DECLARED_GAUGES",
    "DECLARED_HISTOGRAMS",
]
