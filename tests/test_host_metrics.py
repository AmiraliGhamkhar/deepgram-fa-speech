"""Metrics registry tests (host/metrics.py).

No web framework and no network: `host/metrics.py` is deliberately
dependency-free, so these run in the plain test job as well as the host job.

Two of them are regressions for defects that made the registry *lie*:

* `quantiles()` re-accumulated bucket counts that are already cumulative, so
  it reported a p95 orders of magnitude below the measured one -- which
  would let any capacity gate built on it pass a failing host.
* label-cardinality overflow dropped the sample entirely instead of folding
  it into ``other``, so the counters under-reported exactly during the flood
  of fabricated client ids the cap exists to survive.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "host"))

import metrics as metrics_module  # type: ignore[import-not-found]  # noqa: E402


# -- histogram rendering and quantiles -----------------------------------


def test_rendered_histogram_buckets_are_cumulative():
    """The Prometheus text format requires monotonically increasing `le`."""
    registry = metrics_module.Metrics()
    for value in (0.001, 0.02, 0.3, 1.5, 40.0):
        registry.observe("token_request_latency", value)

    rendered = registry.render()
    buckets = []
    for line in rendered.splitlines():
        if line.startswith("token_request_latency_bucket{"):
            le = line.split('le="', 1)[1].split('"', 1)[0]
            count = int(line.rsplit(" ", 1)[1])
            buckets.append((le, count))

    counts = [count for _le, count in buckets]
    assert counts == sorted(counts), f"buckets are not cumulative: {buckets}"
    assert counts[-1] == 5, "the +Inf bucket must equal the observation count"
    # 40.0 is above the largest finite boundary (30.0), so the finite
    # buckets must stop at 4 and only +Inf carries the fifth.
    assert dict(buckets)["30"] == 4
    assert dict(buckets)["+Inf"] == 5


def test_quantiles_do_not_double_count_cumulative_buckets():
    """Regression: p95 used to be reported as 0.05s for this exact sample."""
    registry = metrics_module.Metrics()
    for value in (0.001, 0.02, 0.3, 1.5, 40.0):
        registry.observe("token_request_latency", value)

    result = registry.quantiles("token_request_latency")
    # The median of the sample is 0.3, which falls in the (0.25, 0.5] bucket.
    assert result["p50"] == 0.5
    # 0.95 * 5 = 4.75 -> only the slowest observation (40.0) satisfies it,
    # and it is above the largest boundary, so the estimate saturates there
    # rather than claiming a latency the evidence does not support.
    assert result["p95"] == 30.0
    assert result["p99"] == 30.0


def test_quantiles_report_a_fast_sample_as_fast():
    registry = metrics_module.Metrics()
    for _ in range(10):
        registry.observe("token_request_latency", 0.02)
    assert registry.quantiles("token_request_latency")["p95"] == 0.025


def test_quantiles_merge_label_sets_without_losing_samples():
    registry = metrics_module.Metrics()
    registry.observe("token_request_latency", 0.02, client_id="doctor-01")
    registry.observe("token_request_latency", 5.0, client_id="doctor-02")
    result = registry.quantiles("token_request_latency")
    # Both label sets contribute: with two observations, p50 lands on the
    # bucket holding the faster one and p99 on the bucket holding the slower
    # one. If merging dropped a series, p99 would report the fast bucket.
    assert result["p50"] == 0.025
    assert result["p99"] == 5.0


def test_quantiles_of_an_unobserved_series_are_nan():
    registry = metrics_module.new_metrics()
    result = registry.quantiles("token_request_latency")
    assert math.isnan(result["p50"]) and math.isnan(result["p99"])


# -- bounded label cardinality -------------------------------------------


def test_label_overflow_folds_into_other_instead_of_dropping_the_sample():
    registry = metrics_module.Metrics(max_labelled_clients=2)
    registry.increment("session_success_total", client_id="doctor-01")
    registry.increment("session_success_total", client_id="doctor-02")
    # Past the cap: counted, but under the shared `other` label.
    for index in range(5):
        registry.increment("session_success_total", client_id=f"fabricated-{index}")

    assert registry.tracked_client_labels == 2, "fabricated ids must not create series"
    assert registry.counter_value("session_success_total", client_id="doctor-01") == 1
    assert registry.counter_value("session_success_total", client_id="doctor-02") == 1
    assert registry.counter_value(
        "session_success_total", client_id=metrics_module._OVERFLOW_LABEL  # noqa: SLF001
    ) == 5, "over-cap samples were dropped, so the counter under-reported"
    assert registry.counter_value("session_success_total") == 0, "unlabelled series untouched"


def test_unlabelled_series_are_never_bounded():
    registry = metrics_module.Metrics(max_labelled_clients=1)
    for _ in range(10):
        registry.increment("token_requests_total")
    assert registry.counter_value("token_requests_total") == 10


# -- declared series ------------------------------------------------------


def test_declared_series_are_all_observable_by_the_host():
    """A series the host cannot update is a permanently plausible zero.

    `active_sessions`, `audio_queue_depth`, `session_duration` and friends
    describe the *client*: the host mints a token and never sees the stream,
    the audio queue or a transcript. Declaring them published a zero that a
    dashboard reads as "clinic idle" during a fully busy clinic.
    """
    client_side = {
        "active_sessions",
        "deepgram_connections_active",
        "audio_queue_depth",
        "audio_queue_drops_total",
        "session_reconnects_total",
        "session_duration",
        "first_interim_latency",
        "final_transcript_latency",
    }
    declared = set(metrics_module.DECLARED_COUNTERS) | set(metrics_module.DECLARED_GAUGES) | set(
        metrics_module.DECLARED_HISTOGRAMS
    )
    assert not declared & client_side, f"client-only series declared host-side: {declared & client_side}"


def test_new_metrics_preseeds_counters_and_gauges_but_not_histograms():
    """An empty histogram must render nothing, not `_count 0` without buckets."""
    registry = metrics_module.new_metrics()
    rendered = registry.render()
    for name in metrics_module.DECLARED_COUNTERS:
        assert f"\n{name} 0" in f"\n{rendered}" or rendered.startswith(f"{name} 0")
    for name in metrics_module.DECLARED_GAUGES:
        assert f"\n{name} 0" in f"\n{rendered}" or rendered.startswith(f"{name} 0")
    for name in metrics_module.DECLARED_HISTOGRAMS:
        assert f"{name}_count" not in rendered


def test_rendered_output_has_no_exponent_and_escapes_labels():
    registry = metrics_module.Metrics()
    registry.increment("session_success_total", 1, client_id='doc"tor\\\n01')
    registry.observe("token_request_latency", 1e-4)
    rendered = registry.render()
    assert "e-" not in rendered and "E-" not in rendered
    assert 'client_id="doc\\"tor\\\\\\n01"' in rendered


def test_uptime_is_reported():
    registry = metrics_module.new_metrics()
    assert "host_uptime_seconds" in registry.render()
    assert registry.uptime_seconds >= 0.0
