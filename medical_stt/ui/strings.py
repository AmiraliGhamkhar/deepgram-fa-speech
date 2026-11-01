"""Every user-visible string the control window shows, in one place.

The control window used to spell its Persian captions inline at each call
site, so the same sentence existed two or three times with slightly
different wording ("در حال ذخیره…" next to "در حال ذخیره..."), and a change
to one left the others behind. Keeping them here means:

* one sentence has one definition, so the busy/blocked messages cannot drift;
* a future translation only has to touch this file;
* a reviewer can check the wording without reading widget code.

The strings are Persian because the clinicians using the application read
Persian; English appears only in log lines and developer-facing text.

**Nothing here is a security decision.** Wording of an error tells the user
what to fix; it never names a secret, a token or a client id.
"""
from __future__ import annotations

#: Leading status glyph; every status label starts with it.
STATUS_DOT = "\u25cf"


def with_dot(text: str) -> str:
    """Prefix `text` with the status glyph, so no call site reinvents it."""
    return f"{STATUS_DOT} {text}"


# -- window chrome ----------------------------------------------------------
TITLE = "Medical STT"
TOOLTIP_FONT_MIN_SIZE = 7

# -- primary control --------------------------------------------------------
START = "شروع"
STOP = "توقف"

# -- settings form ----------------------------------------------------------
HOST_LABEL = "میزبان (https)"
SECRET_LABEL = "کلید مشترک"
CLIENT_ID_LABEL = "شناسه دستگاه"
ACTION_TEST = "بررسی\nاتصال"
ACTION_RECOVERY = "متن\nبازیابی"
ACTION_SAVE = "ذخیره\nتنظیمات"
GLYPH_TEST = "↗"
GLYPH_RECOVERY = "≡"
GLYPH_SAVE = "✓"
GLYPH_QUIT = "\u2715"

# -- states -----------------------------------------------------------------
READY = with_dot("آماده")
CONNECTING = with_dot("در حال اتصال...")
RECORDING = with_dot("در حال ضبط...")
RECONNECTING = with_dot("اتصال مجدد...")
STOPPING = with_dot("در حال توقف...")
START_FAILED = with_dot("شروع ناموفق")
FAILED = with_dot("خطا")
#: Shown when the bounded join could not finish the session in time.
STOP_INCOMPLETE = with_dot("توقف کامل نشد")
STOP_INCOMPLETE_DETAIL = "جلسه هنوز در حال پایان است."

# -- busy / blocked feedback ------------------------------------------------
ALREADY_STARTING = with_dot("در حال اتصال...")
SAVING_BLOCKS_START = with_dot("در حال ذخیره…؛ برای شروع صبر کنید")
SAVING = with_dot("در حال ذخیره…")
SAVING_ELLIPSIS = with_dot("در حال ذخیره...")
STOP_BLOCKS_SAVE = with_dot("برای ذخیره، ابتدا ضبط را متوقف کنید")
STOP_BLOCKS_TEST = with_dot("برای بررسی اتصال، ابتدا ضبط را متوقف کنید")
STOP_BLOCKS_ACTION = with_dot("برای ادامه، ابتدا عملیات جاری را متوقف کنید")
TEST_IN_FLIGHT = with_dot("در حال بررسی اتصال…")
TESTING = with_dot("بررسی تنظیمات ذخیره‌شده...")
TEST_FAILED = with_dot("اتصال ناموفق")
TEST_OK = with_dot("دریافت مجوز موفق")

# -- save outcomes ----------------------------------------------------------
SAVED = with_dot("تنظیمات ذخیره شد")
SAVED_KEPT_SECRET = with_dot("تنظیمات ذخیره شد (کلید قبلی حفظ شد)")
SAVE_FAILED = with_dot("ذخیره نشد")

# -- configuration error surface -------------------------------------------
CONFIG_ERROR_STATUS = with_dot("خطای پیکربندی")
CONFIG_ERROR_DETAIL = "فایل تنظیمات نامعتبر است؛ آن را اصلاح کنید و دوباره تلاش کنید"

# -- recovery popup ---------------------------------------------------------
RECOVERY_TITLE = "متن بازیابی — فقط در حافظه"
RECOVERY_COPY = "کپی"
RECOVERY_COPY_GLYPH = "⧉"
RECOVERY_CLEAR = "پاک‌کردن"
RECOVERY_CLEAR_GLYPH = "×"
RECOVERY_COPIED = "کپی شد"

#: Phase → status label, so the watch loop does not keep a dict of its own.
PHASE_LABELS = {
    "recording": RECORDING,
    "reconnecting": RECONNECTING,
    "stopping": STOPPING,
}
PHASE_FALLBACK = CONNECTING
