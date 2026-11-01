"""User-facing session messages (Persian), in one place.

Every string a clinician can see while dictating lives here instead of being
spread across the session, watchdog and rendering code. That keeps the wording
reviewable as a set (they are safety-relevant: each one tells the user whether
the text may be incomplete) and makes a future translation a single file.

Tests and `medical_stt.app` refer to these by name, so the *value* is the
contract -- never inline a copy of these strings anywhere else.
"""
from __future__ import annotations

# -- audio / capture -------------------------------------------------------

#: Microphone delivered no frames for the watchdog interval.
MIC_SILENT_WARNING = "میکروفون صدایی ارسال نمی‌کند؛ دستگاه ضبط را بررسی کنید"

#: Audio chunks were captured but never sent (queue overflow).
DATA_LOSS_WARNING = "بخشی از صدا از دست رفت؛ متن را بررسی کنید"

#: The microphone could not be opened, repeatedly.
MICROPHONE_UNAVAILABLE_WARNING = "میکروفون در دسترس نیست؛ دستگاه را بررسی و برنامه را دوباره اجرا کنید"

# -- transport / capacity --------------------------------------------------

#: The speech service (or the host) refused the stream: the concurrency cap.
CAPACITY_WARNING = "ظرفیت سرویس گفتار پر است؛ تا آزاد شدن ظرفیت دوباره تلاش می‌شود"

#: Previous session's network workers never finished.
ABANDONED_WORKERS_WARNING = "اتصال قبلی پایان نیافته است؛ پس از رفع مشکل برنامه را باز کنید"

# -- transcript handling ---------------------------------------------------

#: One utterance exceeded the accumulator's limit.
UTTERANCE_LIMIT_WARNING = "گفتار طولانی است؛ متن بازیابی را بررسی کنید"

#: A single provider result was larger than anything one utterance may hold.
OVERSIZED_UTTERANCE_WARNING = "گفتار بیش از حد طولانی است؛ متن بازیابی را بررسی کنید"

#: The bounded final-transcript queue overflowed: processing fell behind.
TRANSCRIPT_QUEUE_FULL_WARNING = "پردازش عقب افتاده است؛ متن بازیابی را بررسی کنید"

#: A processing stage raised; the whole utterance was retained.
PROCESSING_FAILED_WARNING = "پردازش متن ناموفق بود؛ متن بازیابی را بررسی کنید"

#: The text could not be delivered to the focused application.
INJECTION_FAILED_WARNING = "درج متن ناموفق بود؛ متن بازیابی را بررسی کنید"

#: A clinical token was recognized with low confidence.
LOW_CONFIDENCE_WARNING = "واژه پزشکی با اطمینان پایین؛ متن درج‌شده را بررسی کنید"

#: Injection is still running while the session is being torn down.
INJECTION_PENDING_WARNING = "درج متن هنوز پایان نیافته است؛ منتظر بمانید"

# -- recovery --------------------------------------------------------------

#: Appended to a warning once the recovery buffer has dropped text.
RECOVERY_FULL_SUFFIX = " — ظرفیت بازیابی پر شده؛ بخشی از متن در دسترس نیست"
