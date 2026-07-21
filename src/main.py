import argparse
import importlib
import json
import logging
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv


JST = timezone(timedelta(hours=9), "JST")
ROOT = Path(__file__).resolve().parent
WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_SET = {"mon", "tue", "wed", "thu", "fri"}
WEEKEND_SET = {"sat", "sun"}
ALL_DAYS_SET = set(WEEKDAY_NAMES)
DAY_GROUP_ALIASES = {
    "weekdays": WEEKDAY_SET,
    "weekday": WEEKDAY_SET,
    "weekend": WEEKEND_SET,
    "weekends": WEEKEND_SET,
    "all": ALL_DAYS_SET,
    "daily": ALL_DAYS_SET,
    "everyday": ALL_DAYS_SET,
}
MODULE_ORDER = [
    "stock_nikkei",
    "stock_watchlist",
    "stock_sector",
    "stock_dividend",
    "market_news",
    "news_movers",
    "stock_x_trends",
    "ai_summary",
    "post_x_magazine",
    "stock_range",
    "report_html",
    "mail_gmail",
]
RUN_HISTORY_PATH = ROOT / "state" / "run_history.json"
RUN_LOCK_PATH = ROOT / "state" / "batch.lock"
CONFIG_PATH = ROOT / "config.json"
CONFIG: dict = {}


def setup_logging() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    log_date = datetime.now(JST).strftime("%Y%m%d")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S%z",
        handlers=[
            logging.FileHandler(ROOT / "logs" / f"batch_{log_date}.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run NightlyBatchNotify batch.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run modules regardless of config schedule. Useful for manual checks.",
    )
    parser.add_argument(
        "--schedule",
        type=str,
        default="",
        help="Override the matched schedule time, e.g. 07:00, 09:30, 12:15, 22:45.",
    )
    return parser.parse_args()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config file was not found: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def get_window_minutes() -> int:
    return int(CONFIG.get("batch_window_minutes", 14))


def is_weekday_only_enabled() -> bool:
    return bool(CONFIG.get("batch_weekdays_only", True))


def _default_schedule_days() -> set[str]:
    return set(WEEKDAY_SET) if is_weekday_only_enabled() else set(ALL_DAYS_SET)


def _normalize_days(value) -> set[str]:
    if isinstance(value, str):
        key = value.strip().lower()
        if key in DAY_GROUP_ALIASES:
            return set(DAY_GROUP_ALIASES[key])
        if key in WEEKDAY_NAMES:
            return {key}
        raise ValueError(f"Invalid day value: '{value}'.")
    if isinstance(value, list):
        days: set[str] = set()
        for item in value:
            days |= _normalize_days(item)
        return days
    raise ValueError(f"Invalid days entry: {value!r}.")


def parse_schedule_entries(raw_schedule=None) -> list[dict]:
    """Returns a list of schedule slots, each evaluated in this order:
    time -> days (is this slot active right now) -> modules (what to run
    if so). A slot is {"time": time, "time_str": "HH:MM", "days": set[str],
    "modules": list[str] | None}. modules=None means "run every enabled
    module" (MODULE_ORDER filtered by module_enabled)."""
    if raw_schedule is None:
        raw_schedule = CONFIG.get("batch_schedule", ["07:00"])
    if isinstance(raw_schedule, str):
        raw_schedule = [part.strip() for part in raw_schedule.split(",") if part.strip()]
    if not isinstance(raw_schedule, list):
        raise ValueError("config.json batch_schedule must be a string or list.")

    default_days = _default_schedule_days()
    entries: list[dict] = []
    for item in raw_schedule:
        if isinstance(item, str):
            time_str = item.strip()
            days = default_days
            modules = None
        elif isinstance(item, dict):
            time_str = str(item.get("time") or "").strip()
            days = _normalize_days(item["days"]) if item.get("days") is not None else default_days
            modules = item.get("modules")
            if modules is not None:
                if not isinstance(modules, list):
                    raise ValueError(f"batch_schedule entry for '{time_str}' has a 'modules' value that isn't an array.")
                modules = [str(name).strip() for name in modules if str(name).strip()]
        else:
            raise ValueError(f"Invalid batch_schedule entry: {item!r}.")
        if not time_str:
            raise ValueError(f"batch_schedule entry is missing a time: {item!r}.")
        try:
            hour_text, minute_text = time_str.split(":", 1)
            time_obj = time(hour=int(hour_text), minute=int(minute_text))
        except ValueError as exc:
            raise ValueError(f"Invalid schedule entry: '{time_str}'.") from exc
        entries.append({"time": time_obj, "time_str": time_str, "days": days, "modules": modules})

    if not entries:
        raise ValueError("Schedule must contain at least one HH:MM entry.")
    return entries


def module_enabled(module_name: str) -> bool:
    modules = CONFIG.get("modules", {})
    return bool(modules.get(module_name, False))


def validate_config() -> list[str]:
    errors: list[str] = []
    modules = CONFIG.get("modules")
    if not isinstance(modules, dict):
        errors.append("config.json modules must be an object.")
    else:
        for module in MODULE_ORDER:
            value = modules.get(module)
            if not isinstance(value, bool):
                errors.append(f"config.json modules.{module} must be true or false.")

    try:
        schedule_entries = parse_schedule_entries()
        for entry in schedule_entries:
            if entry["modules"] is None:
                continue
            for module_name in entry["modules"]:
                if module_name not in MODULE_ORDER:
                    errors.append(
                        f"config.json batch_schedule entry '{entry['time_str']}' has unknown module: {module_name}."
                    )
    except ValueError as exc:
        errors.append(str(exc))

    try:
        window = get_window_minutes()
        if window < 0:
            errors.append("config.json batch_window_minutes must be 0 or greater.")
    except ValueError:
        errors.append("config.json batch_window_minutes must be an integer.")
    except TypeError:
        errors.append("config.json batch_window_minutes must be an integer.")

    weekdays_only = CONFIG.get("batch_weekdays_only", True)
    if not isinstance(weekdays_only, bool):
        errors.append("config.json batch_weekdays_only must be true or false.")

    return errors


def parse_schedule(value: str) -> list[time]:
    """Parses a plain comma-separated 'HH:MM' string (used for --schedule)."""
    schedules: list[time] = []
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        try:
            hour_text, minute_text = part.split(":", 1)
            schedules.append(time(hour=int(hour_text), minute=int(minute_text)))
        except ValueError as exc:
            raise ValueError(f"Invalid schedule entry: '{part}'.") from exc
    if not schedules:
        raise ValueError("Schedule must contain at least one HH:MM entry.")
    return schedules


def is_execution_time(now: datetime) -> tuple[bool, str | None]:
    window_minutes = get_window_minutes()
    today_key = WEEKDAY_NAMES[now.weekday()]
    for entry in parse_schedule_entries():
        if today_key not in entry["days"]:
            continue
        scheduled = entry["time"]
        target = now.replace(
            hour=scheduled.hour,
            minute=scheduled.minute,
            second=0,
            microsecond=0,
        )
        diff = now - target
        if timedelta(0) <= diff <= timedelta(minutes=window_minutes):
            return True, entry["time_str"]
    return False, None


def build_run_key(now: datetime, matched_time: str) -> str:
    return f"{now.strftime('%Y-%m-%d')} {matched_time}"


def load_run_history() -> dict:
    if not RUN_HISTORY_PATH.exists():
        return {"completed_runs": []}
    try:
        return json.loads(RUN_HISTORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("Run history is invalid JSON. Starting with an empty history.")
        return {"completed_runs": []}


def was_run_completed(run_key: str) -> bool:
    history = load_run_history()
    return any(item.get("run_key") == run_key for item in history.get("completed_runs", []))


def record_run_completed(run_key: str, started_at: datetime, completed_at: datetime) -> None:
    RUN_HISTORY_PATH.parent.mkdir(exist_ok=True)
    history = load_run_history()
    completed_runs = history.setdefault("completed_runs", [])
    completed_runs.append(
        {
            "run_key": run_key,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
        }
    )
    history["completed_runs"] = completed_runs[-60:]
    RUN_HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def acquire_lock() -> bool:
    RUN_LOCK_PATH.parent.mkdir(exist_ok=True)
    if RUN_LOCK_PATH.exists():
        lock_age = datetime.now(JST) - datetime.fromtimestamp(RUN_LOCK_PATH.stat().st_mtime, tz=JST)
        if lock_age > timedelta(minutes=30):
            logging.warning("Removing stale lock file: %s", RUN_LOCK_PATH)
            release_lock()
    try:
        fd = os.open(str(RUN_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
        lock_file.write(datetime.now(JST).isoformat())
    return True


def release_lock() -> None:
    try:
        RUN_LOCK_PATH.unlink()
    except FileNotFoundError:
        pass


def load_module_runner(module_name: str):
    module = importlib.import_module(f"modules.{module_name}")
    return module.run


def resolve_modules_for_schedule(schedule_key: str | None) -> list[str]:
    if schedule_key:
        try:
            for entry in parse_schedule_entries():
                if entry["time_str"] == schedule_key and entry["modules"] is not None:
                    return [name for name in entry["modules"] if module_enabled(name)]
        except ValueError:
            pass
    return [name for name in MODULE_ORDER if module_enabled(name)]


def run_enabled_modules(schedule_key: str | None = None) -> None:
    selected_modules = resolve_modules_for_schedule(schedule_key)
    module_status = {name: ("on" if name in selected_modules else "off") for name in MODULE_ORDER}

    logging.info("Selected schedule: %s", schedule_key or "default")
    logging.info("Module settings: %s", module_status)
    for name in MODULE_ORDER:
        if name not in selected_modules:
            logging.info("[%s] skipped: disabled", name)
            continue

        try:
            runner = load_module_runner(name)
        except ModuleNotFoundError:
            logging.info("[%s] skipped: module is not implemented yet", name)
            continue

        logging.info("[%s] started", name)
        runner(root=ROOT)
        logging.info("[%s] completed", name)


def main() -> int:
    global CONFIG
    load_dotenv(ROOT / ".env")
    try:
        CONFIG = load_config()
    except Exception as exc:
        setup_logging()
        logging.error("Failed to load config.json: %s", exc)
        return 1
    setup_logging()
    args = parse_args()

    errors = validate_config()
    if errors:
        for error in errors:
            logging.error(error)
        return 1

    now = datetime.now(JST)
    started_at = now
    run_key = None
    schedule_key = None
    if not args.force:
        should_run, matched_time = is_execution_time(now)
        if not should_run:
            logging.info("Skipped: current time is outside schedule window (%s JST).", now.strftime("%H:%M"))
            return 0
        logging.info("Matched schedule: %s JST", matched_time)
        run_key = build_run_key(now, matched_time)
        schedule_key = matched_time
        if was_run_completed(run_key):
            logging.info("Skipped: schedule %s was already completed.", run_key)
            return 0
    else:
        logging.info("Force mode enabled; schedule check skipped.")
        if args.schedule:
            parsed_schedule = parse_schedule(args.schedule)
            if len(parsed_schedule) != 1:
                logging.error("--schedule must contain exactly one HH:MM entry.")
                return 1
            schedule_key = parsed_schedule[0].strftime("%H:%M")
            logging.info("Force schedule override: %s", schedule_key)

    if not acquire_lock():
        logging.info("Skipped: another batch run is already in progress.")
        return 0

    try:
        if schedule_key:
            os.environ["BATCH_SCHEDULE_KEY"] = schedule_key
        else:
            os.environ.pop("BATCH_SCHEDULE_KEY", None)
        run_enabled_modules(schedule_key=schedule_key)
    except Exception:
        logging.exception("Batch failed.")
        return 1
    finally:
        release_lock()

    if run_key:
        completed_at = datetime.now(JST)
        record_run_completed(run_key, started_at, completed_at)
        logging.info("Recorded completed schedule: %s", run_key)

    logging.info("Batch completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
