"""Import-time .env handling in a source checkout and a frozen client."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_DRIVER = """\
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(message)s")
if sys.argv[1] == "frozen":
    sys.frozen = True
from medical_stt import config
print(json.dumps({
    "marker": os.environ.get("MEDICAL_STT_PROBE_MARKER"),
    "app_data_dir": str(config.paths.app_data_dir()),
}))
"""


@pytest.fixture
def install(tmp_path: Path) -> Path:
    app_dir = tmp_path / "install"
    shutil.copytree(_ROOT / "medical_stt", app_dir / "medical_stt")
    (app_dir / "driver.py").write_text(_DRIVER, encoding="utf-8")
    return app_dir


def _drop_env(folder: Path, relocated: Path) -> None:
    (folder / ".env").write_text(
        "MEDICAL_STT_PROBE_MARKER=from-dropped-env\n"
        f"MEDICALSTT_APP_DATA_DIR={relocated}\n",
        encoding="utf-8",
    )


def _run(install: Path, *, frozen: bool, cwd: Path) -> tuple[dict[str, str | None], str]:
    # The driver must be a script file: python-dotenv uses cwd for `python -c`.
    # Strip *every* inherited MEDICAL*/DEEPGRAM* setting from the child.
    clean_env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("MEDICAL", "DEEPGRAM"))}
    result = subprocess.run(
        [sys.executable, str(install / "driver.py"), "frozen" if frozen else "source"],
        cwd=cwd, env=clean_env, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout), result.stderr


def test_frozen_ignores_env_beside_executable_even_from_other_cwd(install: Path, tmp_path: Path) -> None:
    relocated = tmp_path / "redirected"
    _drop_env(install, relocated)
    result, logs = _run(install, frozen=True, cwd=tmp_path)
    assert result["marker"] is None
    assert result["app_data_dir"] != str(relocated)
    assert str(install / ".env") not in logs


def test_frozen_ignores_env_beside_executable_from_app_cwd(install: Path, tmp_path: Path) -> None:
    relocated = tmp_path / "redirected"
    _drop_env(install, relocated)
    result, _ = _run(install, frozen=True, cwd=install)
    assert result["marker"] is None
    assert result["app_data_dir"] != str(relocated)


def test_frozen_ignores_env_in_parent_of_launch_cwd(install: Path, tmp_path: Path) -> None:
    relocated = tmp_path / "redirected"
    _drop_env(tmp_path, relocated)
    result, _ = _run(install, frozen=True, cwd=install)
    assert result["marker"] is None
    assert result["app_data_dir"] != str(relocated)


def test_source_loads_only_root_env_and_logs_path_not_values(install: Path, tmp_path: Path) -> None:
    relocated = tmp_path / "redirected"
    _drop_env(install, relocated)
    result, logs = _run(install, frozen=False, cwd=tmp_path)
    assert result == {"marker": "from-dropped-env", "app_data_dir": str(relocated)}
    assert logs.count(str(install / ".env")) == 1
    assert "from-dropped-env" not in logs
    assert str(relocated) not in logs


def test_source_does_not_walk_up_to_ancestor_env(install: Path, tmp_path: Path) -> None:
    relocated = tmp_path / "redirected"
    _drop_env(tmp_path, relocated)
    result, logs = _run(install, frozen=False, cwd=install)
    assert result["marker"] is None
    assert result["app_data_dir"] != str(relocated)
    assert str(tmp_path / ".env") not in logs
