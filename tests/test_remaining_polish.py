"""Explicit P2 behavior and documented operational limitations."""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from host.core import HostSettings
from host.metrics import new_metrics
from medical_stt.app import LiveMedicalSTT
from medical_stt.injection.backend import DryRunBackend
from medical_stt.injection.text_injector import TextInjector
from medical_stt.processing.normalize import normalize
from medical_stt.stt.reconnect import ReconnectPolicy, _positive_jitter

ROOT = Path(__file__).resolve().parents[1]


def test_uptime_is_typed_and_status_metric_is_not_preseeded_unlabelled():
    rendered = new_metrics().render()
    assert '# TYPE host_uptime_seconds gauge\n' in rendered
    assert '\nsession_http_outcomes_total 0\n' not in rendered


@pytest.mark.parametrize('key,value', [('HOST_MAX_TTL_SECONDS', '9999'), ('HOST_MAX_TTL_SECONDS', '1'),
                                       ('HOST_MAX_REQUEST_BODY_BYTES', '1')])
def test_clamped_host_settings_warn_by_name_only(key, value, caplog):
    settings = HostSettings.from_env({'DEEPGRAM_API_KEY': 'fake-test-key', 'HOST_SHARED_SECRET': 'x' * 32,
                                      key: value})
    assert settings
    assert any(key in r.getMessage() and 'clamp' in r.getMessage() for r in caplog.records)


def test_nfkc_fraction_slash_is_canonical():
    assert normalize('½') == '1/2'
    assert normalize('cm² ㎡') == 'cm2 m2'


def test_positive_jitter_accepts_injected_rng():
    assert _positive_jitter(10, ReconnectPolicy(jitter=.5), 60,
                            rng=SimpleNamespace(uniform=lambda lo, hi: hi)) == 15


def test_bidi_has_one_owner_at_injection_boundary():
    assert 'to_injected(' not in inspect.getsource(LiveMedicalSTT._render_and_inject)
    backend = DryRunBackend()
    injector = TextInjector(backend=backend)
    assert injector.type_text('گزارش', add_directional_mark=True)


def test_p2_tradeoffs_are_explicit():
    assert 'delta-seconds only' in (ROOT / 'host/core.py').read_text()
    assert 'public-api-only' in TextInjector.type_delta_from_partial.__doc__.lower()
    assert '25' in (ROOT / 'medical_stt/host_client.py').read_text()
    assert 'watchdog' in (ROOT / 'host/passenger_wsgi.py').read_text().lower()
    assert 'deprecated' in (ROOT / 'host/.env.example').read_text().lower()
