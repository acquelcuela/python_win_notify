# NightlyBatchNotify

Windows Task Scheduler calls `run.bat`. This version runs with Windows Python
inside `src\.venv`; Docker is not required.

## Quick Start

1. Review `config.json`.
2. Create `.env` from `.env.example` and edit Gmail settings.
3. Create the virtual environment:

```cmd
setup_windows.bat
```

4. Test manually:

```cmd
.venv\Scripts\python.exe main.py --force
```

5. Register the scheduled task by double-clicking:

```text
scheduler\install_scheduled_task.bat
```

## Scheduler Helpers

- Register: `scheduler\install_scheduled_task.bat`
- Remove: `scheduler\uninstall_scheduled_task.bat`
- Check: `scheduler\check_scheduled_task.bat`

The registered task runs every 15 minutes. Processing still happens only inside
the `.env` schedule windows.

## Docs

- Current specification: `docs\specification.md`
- Progress and decisions: `docs\progress_notes.md`
