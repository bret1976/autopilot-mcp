"""Locked 8-step setup. Every new company starts blank. Never assume 6Frame."""

from __future__ import annotations

from typing import Any

from app.config import DEFAULT_DAILY_HOUR, DEFAULT_DAILY_TIMEZONE, STUDIO_NAME, STUDIO_WEBSITE

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

STEP_READY = 1
STEP_WEBSITE = 2
STEP_BRAND = 3
STEP_APIS = 4
STEP_SCHEDULE = 5
STEP_CONFIRM = 6
STEP_CHOOSE = 7
STEP_RUN = 8

ASK_WEBSITE = "ask_website"
ASK_APIS = "ask_apis"
ASK_SCHEDULE = "ask_schedule"
CONFIRM_SCHEDULE = "confirm_schedule"
CHOOSE_START = "choose_start"
RUNNING = "running"

START_NOW = "now"
START_SCHEDULED = "scheduled"
START_BOTH = "now_and_scheduled"

START_CHOICES = [
    {"id": START_NOW, "label": "Autopost Right Now"},
    {"id": START_SCHEDULED, "label": "Start AutoPost at scheduled times"},
    {
        "id": START_BOTH,
        "label": "Autopost right now and then start automation for scheduled times",
    },
]

SUGGESTED_HOURS = [8, 17]
SUGGESTED_TIMES_PER_DAY = 2

HOST_RULES = [
    "Repeat say_to_user verbatim. Do not paraphrase.",
    "Never mention a leftover brand. This setup is blank until the user pastes a website in this chat.",
    "Never open a Cloud Browser. Never start a second Autopilot conversation. Never say tell me done.",
    "Collect Gemini, PostProxy key, and PostProxy profile group in THIS chat via the setup tool.",
    "Do not call status first. Do not browse websites. Do not draft posts. Do not invent keys.",
    "Render choices as buttons. Call the tool named in next_tool.",
]


def has_brand(record: dict[str, Any]) -> bool:
    return bool(
        str(record.get("brand_name") or "").strip()
        or str(record.get("website_url") or "").strip()
        or str(record.get("brand_voice") or "").strip()
    )


def has_apis(record: dict[str, Any]) -> bool:
    return all(
        str(record.get(key) or "").strip()
        for key in ("gemini_api_key", "postproxy_api_key", "postproxy_profile_group_id")
    )


def schedule_set_by_user(record: dict[str, Any]) -> bool:
    return bool(record.get("schedule_set_by_user"))


def schedule_confirmed(record: dict[str, Any]) -> bool:
    return bool(record.get("schedule_confirmed"))


def start_mode(record: dict[str, Any]) -> str:
    return str(record.get("start_mode") or "").strip()


def infer_step(record: dict[str, Any]) -> str:
    stored = str(record.get("walkthrough_step") or "").strip()
    if not has_brand(record):
        return ASK_WEBSITE
    if not has_apis(record):
        return ASK_APIS
    if not schedule_set_by_user(record):
        return ASK_SCHEDULE
    if not schedule_confirmed(record):
        return CONFIRM_SCHEDULE
    if not start_mode(record):
        return CHOOSE_START
    if stored in {ASK_WEBSITE, ASK_APIS, ASK_SCHEDULE, CONFIRM_SCHEDULE, CHOOSE_START, RUNNING}:
        return stored if stored == RUNNING else RUNNING
    return RUNNING


def step_number(name: str) -> int:
    return {
        ASK_WEBSITE: STEP_WEBSITE,
        ASK_APIS: STEP_APIS,
        ASK_SCHEDULE: STEP_SCHEDULE,
        CONFIRM_SCHEDULE: STEP_CONFIRM,
        CHOOSE_START: STEP_CHOOSE,
        RUNNING: STEP_RUN,
    }.get(name, STEP_WEBSITE)


def format_clock(hour: int) -> str:
    hour = max(0, min(int(hour), 23))
    suffix = "AM" if hour < 12 else "PM"
    shown = hour % 12 or 12
    return f"{shown}:00 {suffix}"


def _hours(record: dict[str, Any]) -> list[int]:
    raw = record.get("daily_run_hours")
    if isinstance(raw, list) and raw:
        out: list[int] = []
        for item in raw:
            try:
                out.append(max(0, min(int(item), 23)))
            except (TypeError, ValueError):
                continue
        if out:
            return sorted(set(out))
    try:
        hour = int(record.get("daily_run_hour") if record.get("daily_run_hour") is not None else DEFAULT_DAILY_HOUR)
    except (TypeError, ValueError):
        hour = DEFAULT_DAILY_HOUR
    return [max(0, min(hour, 23))]


def _days(record: dict[str, Any]) -> list[int]:
    raw = record.get("daily_run_days")
    if raw in (None, "", "everyday", "every day", "daily", "all", "every"):
        return list(range(7))
    if raw in ("weekdays", "weekday"):
        return [0, 1, 2, 3, 4]
    if raw in ("weekends", "weekend"):
        return [5, 6]
    if isinstance(raw, list):
        days: list[int] = []
        for item in raw:
            if isinstance(item, int):
                days.append(max(0, min(item, 6)))
                continue
            short = str(item).strip().lower()[:3]
            if short in _WEEKDAYS:
                days.append(_WEEKDAYS.index(short))
        return sorted(set(days)) if days else list(range(7))
    return list(range(7))


def format_times(record: dict[str, Any]) -> str:
    hours = _hours(record)
    tz = str(record.get("daily_run_timezone") or DEFAULT_DAILY_TIMEZONE)
    times = " and ".join(format_clock(hour) for hour in hours)
    days = _days(record)
    if days == list(range(7)):
        day_bit = "every day"
    elif days == [0, 1, 2, 3, 4]:
        day_bit = "weekdays"
    elif days == [5, 6]:
        day_bit = "weekends"
    else:
        day_bit = ", ".join(_WEEKDAYS[i] for i in days)
    return f"{day_bit} at {times} ({tz})"


def suggested_schedule_line() -> str:
    times = " and ".join(format_clock(hour) for hour in SUGGESTED_HOURS)
    return (
        f"Suggested: twice a day — {times} ({DEFAULT_DAILY_TIMEZONE}). "
        "Twice a day is the default unless they pick something else."
    )


def hours_for_times_per_day(count: int) -> list[int]:
    n = max(1, min(int(count), 8))
    if n == 1:
        return [8]
    if n == 2:
        return [8, 17]
    if n == 3:
        return [8, 12, 17]
    span = 16
    start = 7
    return sorted({min(23, start + round(i * span / (n - 1))) for i in range(n)})


def normalize_start_mode(value: str) -> str:
    text = (value or "").strip().lower()
    compact = text.replace(" ", "_").replace("-", "_")
    if compact in {START_NOW, "autopost_right_now", "right_now", "now_only", "1"}:
        return START_NOW
    if compact in {START_SCHEDULED, "scheduled", "schedule_only", "wait", "2"}:
        return START_SCHEDULED
    if compact in {
        START_BOTH,
        "both",
        "now_and_schedule",
        "now_then_scheduled",
        "3",
    }:
        return START_BOTH
    if "right now" in text and "scheduled" in text:
        return START_BOTH
    if "scheduled" in text:
        return START_SCHEDULED
    if "right now" in text or text == "now":
        return START_NOW
    return ""


def _brand_label(record: dict[str, Any]) -> str:
    name = str(record.get("brand_name") or "").strip()
    return name or "your company"


def say_for_step(record: dict[str, Any], step: str) -> str:
    if step == ASK_WEBSITE:
        return (
            "Step 1 — TrendPilot is loaded and ready.\n\n"
            "Step 2 — Paste your company website. That site becomes the brand for this setup.\n\n"
            "Do not assume a brand. Do not mention any previous company. "
            "Do not open a Cloud Browser. Do not browse the site yourself."
        )
    if step == ASK_APIS:
        brand = _brand_label(record)
        site = str(record.get("website_url") or "").strip()
        site_bit = f" ({site})" if site else ""
        return (
            f"Step 3 — Branding for {brand}{site_bit} is now confirmed.\n\n"
            "Step 4 — Paste these three APIs in this conversation "
            "(not in a Cloud Browser, not in another chat):\n"
            "1. Gemini API key — https://aistudio.google.com/apikey\n"
            "2. PostProxy API key — https://postproxy.dev\n"
            "3. PostProxy profile group ID\n\n"
            "Call setup with those three values. Keys are never printed back."
        )
    if step == ASK_SCHEDULE:
        return (
            "Step 5 — The three APIs are saved.\n\n"
            "What time of day should Autopost run, and how many times per day?\n"
            f"{suggested_schedule_line()}\n\n"
            "Call set_automation with those times. Do not post yet. Do not run Autopilot yet."
        )
    if step == CONFIRM_SCHEDULE:
        return (
            f"Step 6 — Schedule draft: {format_times(record)}.\n\n"
            "Click Confirm schedule to lock these automation times in. "
            "You can change the times later with set_automation.\n\n"
            "Do not post yet."
        )
    if step == CHOOSE_START:
        return (
            f"Step 7 — Automations are set: {format_times(record)}. "
            "You can change these times at any time.\n\n"
            "Choose how to start:\n"
            "1) Autopost Right Now\n"
            "2) Start AutoPost at scheduled times\n"
            "3) Autopost right now and then start automation for scheduled times\n\n"
            "Call choose_start with mode now, scheduled, or now_and_scheduled. "
            "Do not invent a fourth option."
        )
    mode = start_mode(record)
    if mode == START_SCHEDULED:
        return (
            f"Step 8 — Autopost will wait for the scheduled times: {format_times(record)}. "
            "Nothing posts until then. You can change times with set_automation."
        )
    if mode == START_NOW:
        return (
            "Step 8 — Autopost Right Now. Call run_autopilot with draft=false. "
            "This is a live post, not a draft. Recurring automation stays off "
            "unless they also chose scheduled times."
        )
    if mode == START_BOTH:
        return (
            "Step 8 — Autopost right now, then keep the scheduled times. "
            "Call run_autopilot with draft=false now. "
            f"Later posts fire {format_times(record)}."
        )
    return (
        "Step 8 — Start Autopost from the choice they already made. "
        "Never invent a clip. Never post caption-only."
    )


def choices_for_step(step: str) -> list[dict[str, str]]:
    if step == CONFIRM_SCHEDULE:
        return [{"id": "confirm_schedule", "label": "Confirm schedule"}]
    if step == CHOOSE_START:
        return list(START_CHOICES)
    return []


def next_tool_for_step(step: str) -> str:
    return {
        ASK_WEBSITE: "onboard",
        ASK_APIS: "setup",
        ASK_SCHEDULE: "set_automation",
        CONFIRM_SCHEDULE: "confirm_schedule",
        CHOOSE_START: "choose_start",
        RUNNING: "run_autopilot",
    }.get(step, "onboard")


def next_actions(step: str) -> list[str]:
    return {
        ASK_WEBSITE: [
            "Say Step 1 and Step 2 verbatim.",
            "Wait for the company website. Then call onboard(website_url=that_url).",
        ],
        ASK_APIS: [
            "Say branding is confirmed for THIS company only.",
            "Collect the three APIs in this chat and call setup.",
        ],
        ASK_SCHEDULE: [
            "Ask times and how many times per day. Always suggest twice a day.",
            "Call set_automation. Do not run Autopilot yet.",
        ],
        CONFIRM_SCHEDULE: [
            "Show the Confirm schedule button.",
            "Call confirm_schedule when they click it.",
        ],
        CHOOSE_START: [
            "Show the three start buttons.",
            "Call choose_start with the option they pick.",
        ],
        RUNNING: [
            "If they chose now or now_and_scheduled, run_autopilot(draft=false).",
            "If they chose scheduled only, wait for those times.",
        ],
    }.get(step, [])


def mentions_leftover_studio(text: str) -> bool:
    blob = (text or "").lower()
    return "6frame" in blob or "6framestudio" in blob


def redact_studio(text: str) -> str:
    if not mentions_leftover_studio(text):
        return text
    cleaned = text.replace(STUDIO_NAME, "this company").replace(STUDIO_WEBSITE, "the company website")
    return cleaned.replace("6Frame Studio", "this company").replace("6framestudio.com", "the company website")


def walkthrough_view(record: dict[str, Any]) -> dict[str, Any]:
    step = infer_step(record)
    say = say_for_step(record, step)
    if step == ASK_WEBSITE:
        say = redact_studio(say)
    return {
        "step": step_number(step) if step != ASK_WEBSITE else STEP_READY,
        "step_name": step,
        "steps": {
            "1": "Confirm TrendPilot is loaded and ready.",
            "2": "Ask for the company website.",
            "3": "Confirm branding for that company.",
            "4": "Ask for the three APIs.",
            "5": "Ask times and how many times per day (suggest twice a day).",
            "6": "Confirm schedule. Automations are set. Times can change later.",
            "7": "Show the three start options.",
            "8": "Run now, wait for the schedule, or both.",
        },
        "verbatim": True,
        "say_to_user": say,
        "choices": choices_for_step(step),
        "next_tool": next_tool_for_step(step),
        "next_after_keys": next_actions(step),
        "host_rules": list(HOST_RULES),
        "times_per_day": len(_hours(record)) if schedule_set_by_user(record) else SUGGESTED_TIMES_PER_DAY,
        "suggested_times_per_day": SUGGESTED_TIMES_PER_DAY,
        "suggested_hours": list(SUGGESTED_HOURS),
    }


def first_step_config(config: dict[str, Any]) -> dict[str, Any]:
    """First-run payload must not leak a leftover brand."""
    clean = dict(config)
    clean["brand_name"] = None
    clean["website_url"] = None
    clean["brand_voice"] = None
    clean["brand_hashtags"] = []
    clean["last_run"] = None
    clean["automation_enabled"] = False
    clean["schedule_confirmed"] = False
    clean["start_mode"] = None
    return clean
