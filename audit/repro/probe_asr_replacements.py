"""Does load_asr_replacements() enforce the README's 'no units / no English
targets' claim for ASCII-only source pairs?"""
import sys
import tempfile
import pathlib
sys.path.insert(0, ".")
import medical_stt.config as cfg

cases = {
    "ascii_unit_target": "replacements:\n  - from: \"mg\"\n    to: \"mcg\"\n",
    "ascii_english_target": "replacements:\n  - from: \"iv\"\n    to: \"IV\"\n",
    "persian_unit_target": "replacements:\n  - from: \"\u0645\u06cc\u0644\u06cc\u06af\u0631\u0645\"\n    to: \"mg\"\n",
    "digit_source": "replacements:\n  - from: \"d5w\"\n    to: \"dextrose\"\n",
}
original = cfg._DATA_DIR
try:
    for name, body in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            (d / "asr_replacements.yaml").write_text(body, encoding="utf-8")
            cfg._DATA_DIR = d
            try:
                result = cfg.load_asr_replacements()
                print(f"{name:22s} ACCEPTED -> {result}")
            except cfg.ConfigError as exc:
                print(f"{name:22s} REJECTED -> {exc}")
finally:
    cfg._DATA_DIR = original
