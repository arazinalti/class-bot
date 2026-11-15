"""Load project-specific settings from config.json and environment variables."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

BASE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("CLASS_BOT_CONFIG", str(BASE / "config.json")))
load_dotenv(BASE / ".env")


def _load() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {CONFIG_PATH}: {exc}") from exc


_CONFIG = _load()


def _section(name: str) -> dict:
    value = _CONFIG.get(name, {})
    return value if isinstance(value, dict) else {}


personal = _section("personal")
notifications = _section("notifications")
lms = _section("lms")
browser = _section("browser")
timing = _section("timing")

MY_NAMES = [str(x) for x in personal.get("names", [])]
KEYWORDS = [str(x) for x in personal.get("keywords", [])]
NTFY_SERVER = str(notifications.get("server", os.getenv("NTFY_SERVER", "https://ntfy.sh"))).rstrip("/")
NTFY_TOPIC = str(notifications.get("topic", os.getenv("NTFY_TOPIC", ""))).strip()

LMS_BASE_URL = str(lms.get("base_url", os.getenv("LMS_BASE_URL", ""))).rstrip("/")
LMS_HOST = urlparse(LMS_BASE_URL).hostname or ""
LMS_URL = f"{LMS_BASE_URL}/my/" if LMS_BASE_URL else ""
COURSE_URL_TEMPLATE = str(lms.get("course_url_template", "{base_url}/course/view.php?id={{}}"))
COURSE_URL = COURSE_URL_TEMPLATE.replace("{base_url}", LMS_BASE_URL)

COURSES = {}
for item in _CONFIG.get("courses", []):
    if isinstance(item, dict) and item.get("id") is not None and item.get("name"):
        COURSES[int(item["id"])] = str(item["name"])

SCHEDULE = []
for item in _CONFIG.get("schedule", []):
    if not isinstance(item, dict):
        continue
    try:
        SCHEDULE.append((
            str(item["weekday"]).lower(),
            str(item["start"]),
            str(item["end"]),
            int(item["course_id"]),
            item.get("parity"),
        ))
    except (KeyError, TypeError, ValueError):
        raise SystemExit(f"Invalid schedule entry in {CONFIG_PATH}: {item!r}")

EVEN_WEEK_SATURDAY = timing.get("even_week_saturday")
if EVEN_WEEK_SATURDAY:
    from datetime import date
    EVEN_WEEK_SATURDAY = date.fromisoformat(str(EVEN_WEEK_SATURDAY))

JOIN_EARLY_MIN = int(timing.get("join_early_minutes", 10))
END_BUFFER_MIN = int(timing.get("end_buffer_minutes", 30))
BROWSER_CHANNEL = str(browser.get("channel", os.getenv("BROWSER_CHANNEL", "msedge")))

