Patch (copy/paste)

Add these imports near the top:

import re
from dateutil import parser as dateparser, tz
import os
LOCAL_TZ = tz.gettz(os.getenv("LOCAL_TZ", "Asia/Singapore"))


Replace your existing _parse_iso with this flexible version:

_time_only_re = re.compile(r'^\s*(\d{1,2})(?::\d{2})?\s*(am|pm)?\s*$', re.I)
def _looks_like_time_only(s: str) -> bool:
    return bool(_time_only_re.match(s.strip()))

def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    """Accept strict ISO first; fall back to human inputs like '3pm', 'tomorrow 8am'."""
    if not s:
        return None
    # 1) strict ISO
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        pass
    # 2) lenient human parsing
    try:
        # default date = today in LOCAL_TZ at 00:00
        default_dt = datetime.now(tz=LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        dt = dateparser.parse(s, fuzzy=True, default=default_dt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        # If user supplied only a time (e.g., "3pm") and that time today has passed, roll to tomorrow
        if _looks_like_time_only(s) and dt < datetime.now(LOCAL_TZ):
            dt = dt + timedelta(days=1)
        return dt
    except Exception:
        return None


In add_doctor_appointment(...), keep your current call to _parse_iso(start_iso)—it now handles both ISO and human inputs. Store the parsed time back as ISO with timezone:

start_dt = _parse_iso(start_iso)
if not start_dt:
    return {"ok": False, "error": "invalid start_iso (ISO-8601, e.g. 2025-09-12T10:30:00+08:00)"}

appt = {
    ...
    "start": start_dt.isoformat(),  # normalized
    "end": end_iso,
    ...
}