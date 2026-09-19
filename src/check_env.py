"""Environment smoke test for a freshly set-up PC (or after adding a
dependency). Imports every batch module under this Python/venv WITHOUT
calling any module's run() - so it needs no .env/API keys and makes no
network calls or writes - and reports which ones fail to import. This
catches the class of problem a PC migration actually risks: a missing
pip package, a wrong Python version, or a syntax feature the new
interpreter doesn't support. It does not check business logic, API
credentials, or network access - modules/mail_gmail.py send a real test
email for that.

Usage: .venv\\Scripts\\python.exe check_env.py
"""

from __future__ import annotations

import importlib
import sys
import traceback

from main import MODULE_ORDER


def main() -> int:
    failures: list[str] = []
    for name in MODULE_ORDER:
        try:
            importlib.import_module(f"modules.{name}")
        except Exception:
            failures.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()
            print()
        else:
            print(f"OK   {name}")

    print()
    if failures:
        print(f"{len(failures)}/{len(MODULE_ORDER)} module(s) failed to import: {', '.join(failures)}")
        return 1
    print(f"All {len(MODULE_ORDER)} modules imported successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
