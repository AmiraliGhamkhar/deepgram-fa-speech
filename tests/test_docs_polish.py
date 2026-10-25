"""P2 documentation/metrics polish must stay in sync with the code.

The P1 hardening changed operational behavior and added host series. These pins
keep the operator-facing documents and `host/metrics.py`'s module docstring
from silently drifting away from what the code actually emits.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROOT_README = ROOT / "README.md"
HOST_README = ROOT / "host" / "README.md"
METRICS_SOURCE = ROOT / "host" / "metrics.py"
PERFORMANCE_FIXES = ROOT / "docs" / "PERFORMANCE_FIXES.md"


def _normalized(path: Path) -> str:
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8")).lower()


def _metrics_table() -> str:
    text = HOST_README.read_text(encoding="utf-8")
    start = text.index("Published series")
    end = text.index("Alerts worth having", start)
    return text[start:end]


def _declared_metric_names() -> set[str]:
    tree = ast.parse(METRICS_SOURCE.read_text(encoding="utf-8"))
    constants = {
        "DECLARED_COUNTERS",
        "DECLARED_GAUGES",
        "DECLARED_HISTOGRAMS",
    }
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
            if (
                isinstance(target, ast.Name)
                and target.id in constants
                and value is not None
            ):
                names.update(ast.literal_eval(value))
        elif isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id in constants
                for target in node.targets
            ):
                names.update(ast.literal_eval(node.value))
    assert names, "the metrics declaration scan found nothing"
    return names


def test_follow_up_readme_describes_the_new_operational_behavior():
    text = _normalized(ROOT_README)
    assert "two consecutive" in text and "microphone" in text
    assert "per-utterance latency tracker" in text
    assert "neutral" in text and "rtl" in text
    assert "atomic" in text and "registry" in text
    assert "drained" in text and "audio" in text


def test_host_registry_docs_describe_atomic_replacement():
    text = _normalized(HOST_README)
    assert "atomic" in text
    assert "temporary file" in text or "temp file" in text
    assert "os.replace" in text


def test_metrics_table_lists_every_series_the_process_exports():
    table = _metrics_table()
    for name in _declared_metric_names() | {"host_uptime_seconds"}:
        assert name in table, f"{name} is missing from the host metrics table"


def test_metrics_module_docstring_lists_every_series_the_process_exports():
    docstring = ast.get_docstring(ast.parse(METRICS_SOURCE.read_text(encoding="utf-8"))) or ""
    for name in _declared_metric_names() | {"host_uptime_seconds"}:
        assert name in docstring, f"{name} is missing from the metrics module docstring"


def test_performance_fixes_links_the_latest_checkpoint():
    text = _normalized(PERFORMANCE_FIXES)
    assert "2026-10-25 follow-ups" in text
    assert "engineering-checkpoint-2026-10-25-v2.md" in text
