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


def get_schedule_value() -> str:
    schedule = CONFIG.get("batch_schedule", ["00:00", "07:00"])
    if isinstance(schedule, str):
        return schedule
    if isinstance(schedule, list):
        return ",".join(str(item) for item in schedule)
    raise ValueError("config.json batch_schedule must be a string or list.")


def get_window_minutes() -> int:
    return int(CONFIG.get("batch_window_minutes", 14))


def is_weekday_only_enabled() -> bool:
    return bool(CONFIG.get("batch_weekdays_only", True))


def is_japanese_weekday(now: datetime) -> bool:
    return now.weekday() < 5


def module_enabled(module_name: str) -> bool:
    modules = CONFIG.get("modules", {})
    return bool(modules.get(module_name, False))


def _configured_schedule_modules() -> dict[str, list[str]]:
    raw = CONFIG.get("schedule_modules", {})
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, list[str]] = {}
    for key, value in raw.items():
        if not isinstance(value, list):
            continue
        modules = [str(item).strip() for item in value if str(item).strip()]
        if modules:
            normalized[str(key).strip()] = modules
    return normalized


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

    schedule_modules = CONFIG.get("schedule_modules")
    if schedule_modules is not None:
        if not isinstance(schedule_modules, dict):
            errors.append("config.json schedule_modules must be an object.")
        else:
            valid_schedule_keys = {item.strftime("%H:%M") for item in parse_schedule(get_schedule_value())}
            for schedule_key, module_list in schedule_modules.items():
                if schedule_key not in valid_schedule_keys:
                    errors.append(f"config.json schedule_modules.{schedule_key} is not in batch_schedule.")
                    continue
                if not isinstance(module_list, list):
                    errors.append(f"config.json schedule_modules.{schedule_key} must be an array.")
                    continue
                for module_name in module_list:
                    if str(module_name) not in MODULE_ORDER:
                        errors.append(
                            f"config.json schedule_modules.{schedule_key} contains unknown module: {module_name}."
                        )

    try:
        parse_schedule(get_schedule_value())
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
    for scheduled in parse_schedule(get_schedule_value()):
        target = now.replace(
            hour=scheduled.hour,
            minute=scheduled.minute,
            second=0,
            microsecond=0,
        )
        diff = now - target
        if timedelta(0) <= diff <= timedelta(minutes=window_minutes):
            return True, scheduled.strftime("%H:%M")
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
    schedule_modules = _configured_schedule_modules()
    if schedule_key and schedule_key in schedule_modules:
        return [name for name in schedule_modules[schedule_key] if module_enabled(name)]
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
        if is_weekday_only_enabled() and not is_japanese_weekday(now):
            logging.info("Skipped: weekend run is disabled (%s JST).", now.strftime("%Y-%m-%d %H:%M"))
            return 0
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
