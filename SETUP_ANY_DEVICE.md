# Find My Cars: Any-Device One-Click Setup

## What this gives you
- One-click full run: `run_find_my_cars.bat`
- One-click setup for a new machine (installs deps + scheduler): `setup_on_new_device_one_click.bat`
- Auto-run every 5 hours via Windows Task Scheduler: task name `FindMyCars_FullRun_Every5Hours`

## Steps on a new laptop/server
1. Clone this repo.
2. Add your credentials in `.env` (`PYTHONANYWHERE_USERNAME`, `PYTHONANYWHERE_TOKEN`, etc.).
3. Double-click `setup_on_new_device_one_click.bat`.

That setup script will:
- Create `.venv`
- Install `requirements.txt`
- Install Playwright Chromium
- Create/update the scheduled task (every 5 hours)

## Useful scripts
- Build EXE: `build_find_my_cars_exe.bat`
- Install/update scheduler only: `install_find_my_cars_5h_schedule.bat`
- Remove scheduler: `remove_find_my_cars_5h_schedule.bat`
