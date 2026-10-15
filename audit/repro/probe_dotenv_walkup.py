"""Repro: does the shipped client read a `.env` the docs say it ignores?

Code under test (medical_stt/config.py:173-174, executed at import time):

    load_dotenv(_ROOT / ".env")   # .env next to the executable / repo root
    load_dotenv()                 # python-dotenv search (cwd-based when frozen)

Throwaway bundle-shaped tree under audit/scratch/ (report-only: writes only
under audit/). Only synthetic, non-credential variables are used; no forbidden
opt-in variable is set and no network call is made. The driver is a *script
file*, not `python -c`, because python-dotenv treats a `__main__` without
`__file__` as interactive and switches to a cwd-based search.
"""
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "audit" / "scratch" / "dotenv_walkup"
INSTALL = SANDBOX / "install"
REDIRECTED = SANDBOX / "redirected_profile"
MARKER = "dotenv-was-loaded"
ENV_BODY = (
    "# synthetic probe file - no real values\n"
    f"MEDICAL_STT_PROBE_MARKER={MARKER}\n"
    f"MEDICALSTT_APP_DATA_DIR={REDIRECTED}\n"
)
DRIVER = textwrap.dedent(
    """
    import os, sys
    if os.environ.get("PROBE_FROZEN") == "1":
        sys.frozen = True                     # what a Nuitka standalone build sets
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import medical_stt.config as config       # import-time .env loading
    print("    marker loaded        :", os.environ.get("MEDICAL_STT_PROBE_MARKER"))
    print("    app_data_dir()       :", config.paths.app_data_dir())
    """
)
CLEAN_ENV = {k: v for k, v in os.environ.items()
             if not k.startswith(("MEDICAL_STT_", "MEDICALSTT_", "DEEPGRAM_"))}


def build() -> None:
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    (INSTALL).mkdir(parents=True)
    shutil.copytree(ROOT / "medical_stt", INSTALL / "medical_stt")
    shutil.copytree(ROOT / "config", INSTALL / "config")
    (INSTALL / "data").symlink_to(ROOT / "data")
    (INSTALL / "driver.py").write_text(DRIVER, encoding="utf-8")


def run(label: str, *, cwd: Path, frozen: bool) -> None:
    env = dict(CLEAN_ENV)
    env["PROBE_FROZEN"] = "1" if frozen else ""
    out = subprocess.run([sys.executable, str(INSTALL / "driver.py")], cwd=str(cwd),
                         capture_output=True, text=True, env=env)
    print(f"\n{label}\n    cwd={cwd}  sys.frozen={frozen}")
    print(out.stdout.rstrip() or "    (no stdout)")
    if out.returncode:
        print("    stderr:", (out.stderr.strip().splitlines() or [""])[-1])


build()
print(f"bundle-shaped tree: {INSTALL.relative_to(ROOT)}")
print("child env: every MEDICAL_STT_* / MEDICALSTT_* / DEEPGRAM_* variable stripped")

run("A. baseline: no .env anywhere", cwd=Path("/tmp"), frozen=True)

# B: the file the docs say the packaged app ignores, next to the "executable".
(INSTALL / ".env").write_text(ENV_BODY, encoding="utf-8")
run("B. .env inside the app folder, launched from another directory",
    cwd=Path("/tmp"), frozen=True)
run("B2. same file, launched from the app folder (double-clicked exe)",
    cwd=INSTALL, frozen=True)
(INSTALL / ".env").unlink()

(SANDBOX / ".env").write_text(ENV_BODY, encoding="utf-8")
run("C. .env in the PARENT of the app folder, launched from the app folder",
    cwd=INSTALL, frozen=True)
run("D. same .env, frozen but launched from another directory", cwd=Path("/tmp"), frozen=True)
run("E. same .env, NOT frozen (source checkout), launched from the app folder",
    cwd=INSTALL, frozen=False)
(SANDBOX / ".env").unlink()
print("\ncleaned up: no .env left in the probe tree")
