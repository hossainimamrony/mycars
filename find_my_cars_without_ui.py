#!/usr/bin/env python3
"""
Standalone headless runner for Carsales URL matching.

Run:
  python find_my_cars_without_ui.py
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import tempfile
import argparse
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

import pandas as pd
try:
    import requests
except Exception:
    requests = None


def _is_visible(locator: Any) -> bool:
    try:
        if locator.count() <= 0:
            return False
        return bool(locator.first.is_visible())
    except Exception:
        return False


def _challenge_still_visible(root: Any, button_selector: str, track_selector: str) -> bool:
    """Return True only if the challenge elements are definitely still visible.
    If anything fails (frame detached, etc.), assume success."""
    try:
        btn = root.locator(button_selector)
        trk = root.locator(track_selector)
        if _is_visible(btn) and _is_visible(trk):
            return True
        dd = root.locator("#ddv1-captcha-container")
        if dd.count() > 0 and dd.first.is_visible():
            return True
    except Exception:
        # Frame detached or other error ? challenge is gone
        return False
    return False


def _iter_roots(page: Any) -> list[Any]:
    roots: list[Any] = [page]
    try:
        for fr in page.frames:
            if fr is not None:
                roots.append(fr)
    except Exception:
        pass
    return roots


def _human_bezier(t: float) -> float:
    """Easing: fast start, slow middle, fast end (human-like)."""
    if t < 0.5:
        return 4 * t * t * t
    else:
        return 1 - pow(-2 * t + 2, 3) / 2


def _human_drag_full_right(
    page: Any,
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    max_y_deviation: float = 8.0,
) -> None:
    """
    Drag from start to end with human-like curved path, overshoot, and micro-correction.
    """
    distance = end_x - start_x
    if distance <= 0:
        return

    steps = random.randint(40, 70)
    time.sleep(random.uniform(0.05, 0.15))

    prev_x = start_x
    prev_y = start_y

    for i in range(1, steps + 1):
        t = i / steps
        eased_t = _human_bezier(t)

        x = start_x + distance * eased_t
        y_dev = max_y_deviation * math.sin(math.pi * t)
        y_jitter = random.uniform(-2.0, 2.0)
        y = start_y + y_dev + y_jitter
        x += random.uniform(-0.8, 0.8)

        if random.random() < 0.08:
            time.sleep(random.uniform(0.02, 0.07))

        page.mouse.move(x, y)
        time.sleep(random.uniform(0.008, 0.025))

    # Overshoot slightly
    overshoot_x = end_x + random.uniform(3.0, 12.0)
    overshoot_y = end_y + random.uniform(-3.0, 3.0)
    page.mouse.move(overshoot_x, overshoot_y)
    time.sleep(random.uniform(0.04, 0.12))
    # Correct back to exact end
    page.mouse.move(end_x, end_y)
    time.sleep(random.uniform(0.05, 0.15))
    page.mouse.up()


def _drag_once(page: Any, root: Any, button_selector: str, track_selector: str) -> bool:
    btn = root.locator(button_selector).first
    trk = root.locator(track_selector).first

    btn_box = btn.bounding_box()
    trk_box = trk.bounding_box()
    if not btn_box or not trk_box:
        print("? Cannot get bounding boxes for button or track.")
        return False

    start_x = float(btn_box["x"]) + (float(btn_box["width"]) * 0.5)
    start_y = float(btn_box["y"]) + (float(btn_box["height"]) * 0.5)
    track_left = float(trk_box["x"])
    track_width = float(trk_box["width"])
    handle_width = float(btn_box["width"])
    end_x = track_left + track_width - (handle_width / 2)
    end_y = float(trk_box["y"]) + (float(trk_box["height"]) * 0.5)

    if end_x <= start_x:
        print("?? end_x is not to the right ? check track and button positions.")
        return False

    # Human approach
    approach_x = start_x + random.uniform(-30, 30)
    approach_y = start_y + random.uniform(-20, 20)
    page.mouse.move(approach_x, approach_y)
    time.sleep(random.uniform(0.1, 0.3))
    page.mouse.move(start_x, start_y)
    time.sleep(random.uniform(0.1, 0.25))
    page.mouse.down()
    time.sleep(random.uniform(0.08, 0.22))

    _human_drag_full_right(page, start_x, start_y, end_x, end_y, max_y_deviation=random.uniform(5.0, 12.0))

    # Wait a little longer for the page to react
    time.sleep(random.uniform(2.0, 3.0))

    # Success check: if the challenge elements are no longer visible (including frame detached) ? success
    if not _challenge_still_visible(root, button_selector, track_selector):
        print("? Drag successful ? challenge no longer visible.")
        return True

    # Additional check: sometimes the iframe detaches after drag, causing _challenge_still_visible to throw, but we already caught that above.
    # Double check if the main page navigated away from captcha domain.
    try:
        page_url = str(getattr(page, "url", "") or "").lower()
        if "captcha-delivery.com/captcha" not in page_url:
            print("? Drag likely succeeded ? page navigated away from captcha domain.")
            return True
    except Exception:
        pass

    print("?? Drag completed but challenge still visible (may be blocked).")
    return False


def _drag_once_via_locator(root: Any, button_selector: str, track_selector: str) -> bool:
    """Fallback ? not recommended for full?width drags, but kept."""
    btn = root.locator(button_selector).first
    trk = root.locator(track_selector).first
    try:
        btn.drag_to(trk, force=True)
        time.sleep(random.uniform(1.5, 2.5))
    except Exception as e:
        print(f"? Native drag_to failed: {e}")
        return False
    success = not _challenge_still_visible(root, button_selector, track_selector)
    if success:
        print("? Native drag_to succeeded.")
    else:
        print("?? Native drag_to completed but not far enough?")
    return success


def solve_slider(
    *,
    page: Any,
    slider_button_selector: str,
    slider_track_selector: str,
    max_attempts: int = 2,
) -> bool:
    """
    Attempts to solve a slider challenge by dragging the handle all the way to the right.
    Waits 5 seconds before starting.
    """
    attempts = max(1, int(max_attempts or 1))

    print("Waiting 5 seconds before attempting to drag the slider...")
    time.sleep(5)

    roots = _iter_roots(page)

    for attempt in range(1, attempts + 1):
        print(f"\n--- Attempt {attempt}/{attempts} ---")
        found_any = False

        for idx, root in enumerate(roots):
            try:
                btn = root.locator(slider_button_selector)
                trk = root.locator(slider_track_selector)
                btn_visible = _is_visible(btn)
                trk_visible = _is_visible(trk)

                if not (btn_visible and trk_visible):
                    missing = []
                    if not btn_visible:
                        missing.append(f"button '{slider_button_selector}'")
                    if not trk_visible:
                        missing.append(f"track '{slider_track_selector}'")
                    print(f"Frame {idx}: {', '.join(missing)} not visible.")
                    continue

                found_any = True
                print(f"Found both elements in frame {idx}. Dragging full width to the right...")

                if _drag_once(page, root, slider_button_selector, slider_track_selector):
                    return True

                # If drag_once failed, try the native locator alternative
                if _drag_once_via_locator(root, slider_button_selector, slider_track_selector):
                    return True

                print("Both drag strategies failed for this frame.")
            except Exception as e:
                print(f"Error in frame {idx}: {e}")
                continue

        if not found_any:
            print("Could not find a visible slider button and track in any frame. Check your selectors and page state.")

    print(f"\nFailed to solve slider after {attempts} attempt(s).")
    return False


ROOT = Path(__file__).resolve().parent
SCRIPT_PATH = ROOT / "find_my_cars.py"
DEFAULT_INVENTORY = ROOT / "carbarn_inventory.csv.xlsx"
DEFAULT_OUTDIR = ROOT / "find_my_cars_output"
CARBARN_INVENTORY_API_URL = "https://www.cbs.s1.carbarn.com.au/carbarnau/api/v1/vehicles"
PROGRESS_PREFIX = "PROGRESS_JSON:"
MANUAL_URLS_FILENAME = "manual_carsales_urls.json"
CARSALES_URLS_FILENAME = "carsales_urls.json"
CARSALES_URL_REGISTRY_FILENAME = "carsales_url_registry.json"
MANUAL_MATCHES_FILENAME = "manual_matches.json"
MANUAL_PROGRESS_FILENAME = "manual_match_progress.json"
FULL_COMPARISON_FILENAME = "full_comparisons.json"
UI_BUILD = "20260430-1"


def _load_simple_env_file(path: Path) -> None:
    if not path.exists() or not path.is_file():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if not key:
                continue
            if len(val) >= 2 and ((val[0] == val[-1]) and val[0] in {"'", '"'}):
                val = val[1:-1]
            # Keep explicit process env priority; only set if missing.
            if key not in os.environ:
                os.environ[key] = val
    except Exception:
        # Environment file loading should never crash app startup.
        pass


# Load local env files if present so app works when launched directly.
_load_simple_env_file(ROOT / ".env")
_load_simple_env_file(ROOT / ".env.local")

PYTHONANYWHERE_HOST = os.getenv("PYTHONANYWHERE_HOST", "www.pythonanywhere.com").strip() or "www.pythonanywhere.com"
PYTHONANYWHERE_USERNAME = os.getenv("PYTHONANYWHERE_USERNAME", "").strip()
PYTHONANYWHERE_TOKEN = os.getenv("PYTHONANYWHERE_TOKEN", os.getenv("API_TOKEN", "")).strip()
PYTHONANYWHERE_REMOTE_DIR = os.getenv("PYTHONANYWHERE_REMOTE_DIR", "").strip()

class JobState:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc: subprocess.Popen[str] | None = None
        self.running = False
        self.stop_requested = False
        self.return_code: int | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.logs: list[str] = []
        self.max_logs = 5000
        self.last_cmd: list[str] = []
        self.last_outdir = str(DEFAULT_OUTDIR)
        self.last_error = ""
        self.progress: dict[str, Any] = {}
        self.event_seq = 0
        self.progress_updated_at: float | None = None

    def append_log(self, line: str):
        with self.lock:
            self.logs.append(line.rstrip("\n"))
            if len(self.logs) > self.max_logs:
                self.logs = self.logs[-self.max_logs :]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "stop_requested": self.stop_requested,
                "return_code": self.return_code,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "last_cmd": self.last_cmd,
                "last_outdir": self.last_outdir,
                "last_error": self.last_error,
                "progress": self.progress,
                "event_seq": self.event_seq,
                "progress_updated_at": self.progress_updated_at,
            }

    def update_progress(self, payload: dict[str, Any]):
        with self.lock:
            self.progress = dict(payload)
            self.event_seq += 1
            self.progress_updated_at = time.time()


STATE = JobState()
INVENTORY_CACHE: dict[str, Any] = {
    "items": [],
    "cached_at": 0.0,
    "last_error": "",
}

ANTI_BOT_BROWSER_LOCK = threading.Lock()
ANTI_BOT_BROWSER: dict[str, Any] = {
    "playwright": None,
    "browser": None,
    "context": None,
    "page": None,
}
ANTI_BOT_SESSION_LOCK = threading.Lock()
ANTI_BOT_SESSION: dict[str, Any] = {
    "session_pool_dir": None,
    "storage_state_path": None,
    "save_storage_state_on_exit": False,
    "session_reuse_enabled": False,
    "last_saved_epoch": 0.0,
}

SLIDER_SELECTOR_CANDIDATES: list[tuple[str, str]] = [
    ("#captcha__frame__bottom .slider", "#captcha__frame__bottom .sliderTarget"),
    ("#ddv1-captcha-container .sliderContainer .slider", "#ddv1-captcha-container .sliderContainer .sliderTarget"),
    (".slider", ".sliderTarget"),
    ("#ddv1-captcha-container .slider", "#ddv1-captcha-container .sliderTarget"),
    (".slider-button-selector", ".slider-track-selector"),
    (".nc_iconfont.btn_slide", ".nc_scale"),
    (".nc_iconfont.btn_slide", ".nc_scale span"),
    ("#tcaptcha_drag_thumb", "#tcaptcha_drag_track"),
    (".geetest_btn", ".geetest_slider"),
    (".geetest_slider_button", ".geetest_slider"),
]


def _configure_antibot_session_runtime(
    *,
    out_dir: Path,
    session_pool_dir_raw: str = "",
    storage_state_raw: str = "",
    session_reuse_enabled: bool = False,
    save_storage_state_on_exit: bool = False,
) -> dict[str, Any]:
    prev_enabled = True
    with ANTI_BOT_SESSION_LOCK:
        prev_enabled = bool(ANTI_BOT_SESSION.get("session_reuse_enabled", True))
    session_pool_dir = (
        Path(session_pool_dir_raw).expanduser().resolve()
        if str(session_pool_dir_raw or "").strip()
        else (out_dir / "session_pool").resolve()
    )
    session_pool_dir.mkdir(parents=True, exist_ok=True)
    if bool(session_reuse_enabled) and str(storage_state_raw or "").strip():
        storage_state_path: Path | None = Path(storage_state_raw).expanduser().resolve()
    elif bool(session_reuse_enabled):
        candidates = sorted([p for p in session_pool_dir.glob("*.json") if p.is_file()])
        storage_state_path = candidates[0] if candidates else (session_pool_dir / "ui_auto_state.json")
    else:
        storage_state_path = None
    resolved_storage_state = str(storage_state_path) if storage_state_path is not None else ""
    resolved_save_on_exit = bool(save_storage_state_on_exit) and bool(session_reuse_enabled)
    with ANTI_BOT_SESSION_LOCK:
        ANTI_BOT_SESSION["session_pool_dir"] = str(session_pool_dir)
        ANTI_BOT_SESSION["storage_state_path"] = resolved_storage_state
        ANTI_BOT_SESSION["save_storage_state_on_exit"] = resolved_save_on_exit
        ANTI_BOT_SESSION["session_reuse_enabled"] = bool(session_reuse_enabled)
    return {
        "session_pool_dir": str(session_pool_dir),
        "storage_state_path": resolved_storage_state,
        "save_storage_state_on_exit": resolved_save_on_exit,
        "session_reuse_enabled": bool(session_reuse_enabled),
        "mode_changed": prev_enabled != bool(session_reuse_enabled),
    }


def _save_antibot_storage_state(*, reason: str = "", min_interval_seconds: float = 1.5) -> tuple[bool, str]:
    with ANTI_BOT_BROWSER_LOCK:
        context = ANTI_BOT_BROWSER.get("context")
    if context is None:
        return False, "no_context"
    with ANTI_BOT_SESSION_LOCK:
        save_enabled = bool(ANTI_BOT_SESSION.get("save_storage_state_on_exit", True))
        storage_state_path = str(ANTI_BOT_SESSION.get("storage_state_path") or "").strip()
        last_saved_epoch = float(ANTI_BOT_SESSION.get("last_saved_epoch", 0.0) or 0.0)
    if not save_enabled:
        return False, "save_disabled"
    if not storage_state_path:
        return False, "no_storage_state_path"
    now = time.time()
    if (now - last_saved_epoch) < max(0.0, float(min_interval_seconds or 0.0)):
        return False, "save_throttled"
    try:
        p = Path(storage_state_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(p))
        with ANTI_BOT_SESSION_LOCK:
            ANTI_BOT_SESSION["last_saved_epoch"] = now
        why = f" ({reason})" if str(reason or "").strip() else ""
        STATE.append_log(f"[session] storage state saved{why}: {p}")
        return True, str(p)
    except Exception as e:
        why = f" ({reason})" if str(reason or "").strip() else ""
        STATE.append_log(f"[session] storage state save failed{why}: {e}")
        return False, str(e)


def build_command(payload: dict[str, Any], inventory_file: Path) -> tuple[list[str], Path]:
    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    runner_python = str(venv_python) if venv_python.exists() else sys.executable
    out_dir = Path((payload.get("out_dir") or DEFAULT_OUTDIR)).resolve()
    max_pages = str(payload.get("max_pages") or "20").strip()
    workflow_mode = str(payload.get("workflow_mode") or "full").strip().lower()
    verify_on_the_go = bool(payload.get("verify_on_the_go", False))
    search_url = str(payload.get("search_url") or "").strip()
    storage_state = str(payload.get("storage_state") or "").strip()
    session_pool_dir = str(payload.get("session_pool_dir") or "").strip()
    session_health_file = str(payload.get("session_health_file") or "").strip()
    challenge_cooldown_base_seconds = str(payload.get("challenge_cooldown_base_seconds") or "").strip()
    save_storage_state_on_exit = bool(payload.get("save_storage_state_on_exit", False))

    cmd = [runner_python, str(SCRIPT_PATH)]
    cmd += ["--inventory-file", str(inventory_file.resolve())]
    cmd += ["--out-dir", str(out_dir)]
    if search_url:
        cmd += ["--search-url", search_url]
    if max_pages:
        cmd += ["--max-pages", max_pages]
    if workflow_mode in {"full", "identified-only", "not-identified-only"}:
        cmd += ["--workflow-mode", workflow_mode]
    if payload.get("allow_price_only", True):
        cmd += ["--allow-price-only"]
    if not verify_on_the_go:
        cmd += ["--no-verify-on-the-go"]
    if payload.get("headless", False):
        cmd += ["--headless"]
    if payload.get("no_keep_open", False):
        cmd += ["--no-keep-open"]
    if storage_state:
        cmd += ["--storage-state", storage_state]
    if session_pool_dir:
        cmd += ["--session-pool-dir", session_pool_dir]
    if session_health_file:
        cmd += ["--session-health-file", session_health_file]
    if challenge_cooldown_base_seconds:
        cmd += ["--challenge-cooldown-base-seconds", challenge_cooldown_base_seconds]
    if save_storage_state_on_exit:
        cmd += ["--save-storage-state-on-exit"]
    return cmd, out_dir


def run_job(cmd: list[str], out_dir: Path):
    with STATE.lock:
        STATE.running = True
        STATE.return_code = None
        STATE.started_at = time.time()
        STATE.finished_at = None
        STATE.logs = []
        STATE.last_cmd = cmd[:]
        STATE.last_outdir = str(out_dir)
        STATE.last_error = ""
        STATE.progress = {"stage": "init", "event": "run_started", "progress_percent": 0.0}
        STATE.event_seq += 1
        STATE.progress_updated_at = time.time()

    def _fallback_commands(primary: list[str]) -> list[list[str]]:
        if len(primary) < 2:
            return []
        script_and_args = primary[1:]
        fallbacks: list[list[str]] = []
        seen: set[str] = set()

        def add_cmd(parts: list[str]):
            key = " ".join(parts).lower()
            if key in seen:
                return
            seen.add(key)
            fallbacks.append(parts)

        py_launcher = shutil.which("py")
        if py_launcher:
            add_cmd([py_launcher, "-3.12", *script_and_args])
            add_cmd([py_launcher, "-3.11", *script_and_args])

        local_app = Path(os.environ.get("LOCALAPPDATA", ""))
        common = [
            local_app / "Programs" / "Python" / "Python312" / "python.exe",
            local_app / "Programs" / "Python" / "Python311" / "python.exe",
            Path("C:/Python312/python.exe"),
            Path("C:/Python311/python.exe"),
        ]
        for p in common:
            if p.exists():
                add_cmd([str(p), *script_and_args])
        return fallbacks

    def _looks_like_playwright_pipe_error(lines: list[str]) -> bool:
        joined = "\n".join(lines[-240:]).lower()
        return (
            "permissionerror: [winerror 5]" in joined
            and "access is denied" in joined
            and "playwright" in joined
            and "create_subprocess_exec" in joined
        )

    commands_to_try = [cmd] + _fallback_commands(cmd)
    code = 1
    attempt_lines: list[str] = []

    for attempt_idx, current_cmd in enumerate(commands_to_try, 1):
        attempt_lines = []
        STATE.append_log(f"$ {' '.join(current_cmd)}")
        try:
            proc = subprocess.Popen(
                current_cmd,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            with STATE.lock:
                STATE.running = False
                STATE.return_code = -1
                STATE.finished_at = time.time()
                STATE.last_error = str(e)
            STATE.append_log(f"Failed to start process: {e}")
            return

        with STATE.lock:
            STATE.proc = proc

        assert proc.stdout is not None
        for line in proc.stdout:
            attempt_lines.append(line.rstrip("\n"))
            clean = line.rstrip("\n")
            if clean.startswith(PROGRESS_PREFIX):
                raw = clean[len(PROGRESS_PREFIX) :].strip()
                try:
                    payload = json.loads(raw)
                    if isinstance(payload, dict):
                        STATE.update_progress(payload)
                except Exception:
                    STATE.append_log(line)
                continue
            STATE.append_log(line)

        code = proc.wait()
        with STATE.lock:
            STATE.proc = None

        if code == 0:
            break
        if attempt_idx < len(commands_to_try) and _looks_like_playwright_pipe_error(attempt_lines):
            STATE.append_log(
                "Detected Playwright startup permission issue. Retrying with alternate Python runtime..."
            )
            continue
        break

    def _derive_error_message(lines: list[str], code_value: int) -> str:
        for line in reversed(lines[-300:]):
            t = (line or "").strip()
            if not t:
                continue
            if "permissionerror:" in t.lower() and "winerror 5" in t.lower():
                return t
            if "module not found" in t.lower():
                return t
            if "traceback" in t.lower():
                continue
            if t.lower().startswith(("error:", "exception:", "runtimeerror:", "valueerror:", "typeerror:", "permissionerror:")):
                return t
        return f"Process exited with code {code_value}"

    with STATE.lock:
        STATE.running = False
        STATE.return_code = code
        STATE.finished_at = time.time()
        if code != 0:
            STATE.last_error = _derive_error_message(attempt_lines, code)
        STATE.event_seq += 1
        final_progress = dict(STATE.progress or {})
        final_progress.update(
            {
                "stage": "finalize",
                "event": "run_completed" if code == 0 else "run_failed",
                "progress_percent": 100.0 if code == 0 else float(final_progress.get("progress_percent", 0.0)),
            }
        )
        STATE.progress = final_progress
        STATE.progress_updated_at = time.time()
    STATE.append_log(f"Process finished with exit code {code}")


def safe_list_outputs(out_dir: str) -> list[dict[str, Any]]:
    p = Path(out_dir)
    if not p.exists():
        return []
    files: list[dict[str, Any]] = []
    for f in p.glob("*"):
        if f.is_file() and f.suffix.lower() in {".csv", ".xlsx", ".json"}:
            st = f.stat()
            files.append(
                {
                    "name": f.name,
                    "path": str(f),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                }
            )
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return files[:80]


def load_identified_cars(out_dir: str) -> list[dict[str, Any]]:
    p = Path(out_dir)
    if not p.exists():
        return []
    latest = p / "my_cars.csv"
    if not latest.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with latest.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                title = r.get("title", "") or r.get("detail_page_title", "")
                year = r.get("inventory_year_filter", "") or ""
                make = r.get("inventory_make_filter", "") or ""
                model = r.get("inventory_model_filter", "") or ""
                rows.append(
                    {
                        "status": "identified",
                        "title": title,
                        "year": str(year).strip(),
                        "make": str(make).strip(),
                        "model": str(model).strip(),
                        "price_text": r.get("price_text", ""),
                        "price": r.get("price", ""),
                        "odometer_text": r.get("odometer_text", ""),
                        "description": r.get("detail_page_title", "") or title,
                        "detail_url": r.get("detail_url", ""),
                        "image_url": r.get("first_image_url", ""),
                        "all_image_urls": [],
                        "image_count": "",
                        "stock_no": r.get("dealer_stock_id", "") or r.get("matched_stock_no", ""),
                        "dealer_stock_ids": [],
                        "chassis": r.get("matched_chassis", ""),
                        "car_code": r.get("car_code", ""),
                        "photo_count": r.get("photo_count", ""),
                        "vin": r.get("vin", ""),
                        "registration_plate": r.get("registration_plate", ""),
                        "body_type": r.get("body_type", ""),
                        "fuel": r.get("fuel", ""),
                        "transmission": r.get("transmission", ""),
                    }
                )
    except Exception:
        return []
    return rows


def load_skipped_targets(out_dir: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    p = Path(out_dir)
    latest = p / "skipped_targets.csv"
    if not latest.exists():
        return {}
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    try:
        with latest.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                y = _norm_key_text(r.get("year", ""))
                mk = _norm_key_text(r.get("make", ""))
                md = _norm_key_text(r.get("model", ""))
                if not (mk or md or y):
                    continue
                out[(y, mk, md)] = dict(r)
    except Exception:
        return {}
    return out


def _norm_text(v: Any) -> str:
    s = str(v or "").strip()
    if s.lower() == "nan":
        return ""
    return s


def _norm_key_text(v: Any) -> str:
    return _norm_text(v).lower()


def _norm_price(v: Any) -> int | None:
    s = "".join(ch for ch in str(v or "") if ch.isdigit())
    return int(s) if s else None


def _extract_vehicle_year(year_value: Any, title_value: Any = "") -> int | None:
    year_txt = _norm_text(year_value)
    digits = "".join(ch for ch in year_txt if ch.isdigit())
    if len(digits) >= 4:
        try:
            y = int(digits[:4])
            if 1900 <= y <= datetime.now().year + 1:
                return y
        except ValueError:
            pass

    title_txt = _norm_text(title_value)
    for token in title_txt.replace("/", " ").replace("-", " ").split():
        if len(token) == 4 and token.isdigit():
            y = int(token)
            if 1900 <= y <= datetime.now().year + 1:
                return y
    return None


def build_warranty_data(year_value: Any, title_value: Any = "") -> dict[str, Any]:
    year_num = _extract_vehicle_year(year_value, title_value)
    if year_num is None:
        return {
            "vehicle_year": "",
            "vehicle_age": None,
            "rule_label": "Year Not Detected",
            "dealer_warranty": None,
            "integrity_warranty": None,
        }

    vehicle_age = max(0, datetime.now().year - year_num)
    if vehicle_age <= 10:
        # <= 10 years: both warranties available.
        dealer_warranty = True
        integrity_warranty = True
        rule_label = "Up To 10 Years Vehicle"
    elif vehicle_age <= 25:
        # <= 25 years: only 5-years integrity warranty.
        dealer_warranty = False
        integrity_warranty = True
        rule_label = "Up To 25 Years Vehicle"
    else:
        # > 25 years: no warranty.
        dealer_warranty = False
        integrity_warranty = False
        rule_label = "Above 25 Years Vehicle"

    return {
        "vehicle_year": str(year_num),
        "vehicle_age": vehicle_age,
        "rule_label": rule_label,
        "dealer_warranty": dealer_warranty,
        "integrity_warranty": integrity_warranty,
    }


def _pick(obj: dict[str, Any], keys: list[str], default: Any = "") -> Any:
    for k in keys:
        if k in obj and obj[k] not in (None, ""):
            return obj[k]
    return default


def _truncate_text(v: Any, limit: int = 180) -> str:
    s = _norm_text(v)
    if len(s) <= limit:
        return s
    return s[: limit - 3].rstrip() + "..."


def _normalize_image_url(raw_url: str) -> str:
    s = _norm_text(raw_url)
    if not s:
        return ""
    if s.startswith("//"):
        return f"https:{s}"
    if s.startswith("/"):
        # Carbarn assets can come as relative paths.
        return f"https://www.carbarn.com.au{s}"
    return s

def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    return s in {"1", "true", "yes", "y", "on", "published", "online", "live", "active"}


def _normalize_detail_url(raw_url: str) -> str:
    s = _norm_text(raw_url)
    if not s:
        return ""
    if s.startswith("//"):
        return f"https:{s}"
    if s.startswith("/"):
        return f"https://www.carbarn.com.au{s}"
    return s


def _carbarn_detail_url(v: dict[str, Any], stock_no: Any) -> str:
    direct = _pick(
        v,
        [
            "url",
            "listingUrl",
            "publicUrl",
            "websiteUrl",
            "carbarnUrl",
            "vehicleUrl",
            "detailUrl",
            "detailsUrl",
            "seoUrl",
            "webUrl",
            "uri",
            "path",
        ],
        default="",
    )
    if isinstance(direct, dict):
        direct = _pick(direct, ["url", "href", "path"], default="")
    normalized = _normalize_detail_url(str(direct))
    if "/vehicles/" in normalized:
        return normalized

    make_slug = _norm_key_text(_pick(v, ["make", "vehicleMake", "manufacturer"]))
    model_slug = _norm_key_text(_pick(v, ["model", "vehicleModel"]))
    stock_txt = _norm_text(stock_no)
    series_slug = _norm_key_text(_pick(v, ["modelCode", "series", "seriesCode"]))
    if make_slug and model_slug and series_slug and stock_txt:
        return f"https://www.carbarn.com.au/vehicles/{make_slug}/{model_slug}/{series_slug}/{stock_txt}"

    if stock_txt:
        return f"https://www.carbarn.com.au/search?keyword={stock_txt}"
    return ""


def classify_vehicle_scope(v: dict[str, Any], row: dict[str, Any]) -> str:
    # Business rule: "On offer" cars are excluded from Carsales matching scope.
    if _as_bool(_pick(v, ["isOnOffer", "onOffer"], default=False)):
        return "on_offer"

    publish_status = _norm_key_text(_pick(v, ["status", "publishStatus", "websiteStatus"]))
    if publish_status in {"unpublished", "draft", "hidden"}:
        return "unpublished"

    sold_status = _norm_key_text(_pick(v, ["soldStatus", "saleStatus", "status"]))
    if sold_status in {"sold", "archived", "deleted", "inactive"}:
        return "excluded_other"

    explicit_publish_keys = [
        "published",
        "isPublished",
        "publish",
        "isPublish",
        "websitePublished",
        "isWebsitePublished",
        "isOnline",
        "online",
        "isLive",
        "live",
    ]
    has_explicit_flag = any(k in v for k in explicit_publish_keys)
    if has_explicit_flag and not any(_as_bool(v.get(k)) for k in explicit_publish_keys if k in v):
        return "excluded_other"

    detail_url = _norm_text(row.get("detail_url", ""))
    if "/vehicles/" in detail_url:
        return "published_scope"

    website_state = _norm_key_text(_pick(v, ["websiteStatus", "publishStatus", "onlineStatus"]))
    if website_state in {"published", "online", "live", "active"}:
        return "published_scope"

    stock_in = _norm_key_text(_pick(v, ["stockIn", "stockLocation"]))
    if "online" in stock_in:
        return "published_scope"

    return "published_scope" if not has_explicit_flag else "excluded_other"


def is_published_vehicle(v: dict[str, Any], row: dict[str, Any]) -> bool:
    return classify_vehicle_scope(v, row) == "published_scope"

def fetch_carbarn_inventory(force: bool = False) -> tuple[list[dict[str, Any]], str]:
    now = time.time()
    if not force and INVENTORY_CACHE["items"] and (now - float(INVENTORY_CACHE["cached_at"])) < 300:
        return INVENTORY_CACHE["items"], INVENTORY_CACHE["last_error"]

    vehicles: list[dict[str, Any]] = []
    page = 0
    max_pages = 2000
    last_error = ""
    headers = {"accept": "application/json", "user-agent": "Mozilla/5.0"}

    try:
        while page < max_pages:
            url = f"{CARBARN_INVENTORY_API_URL}?page={page}&size=500&sort=id,asc&soldStatus=UnSold&stockIn=Online"
            req = Request(url, headers=headers)
            with urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="ignore"))

            content = payload.get("content") if isinstance(payload, dict) else []
            if not isinstance(content, list) or not content:
                break
            vehicles.extend(content)

            page_info = payload.get("page") if isinstance(payload, dict) else {}
            total_pages = int(page_info.get("totalPages", 0) or 0) if isinstance(page_info, dict) else 0
            page += 1
            if total_pages and page >= total_pages:
                break
    except Exception as e:
        last_error = str(e)

    if vehicles:
        INVENTORY_CACHE["items"] = vehicles
        INVENTORY_CACHE["cached_at"] = now
        INVENTORY_CACHE["last_error"] = last_error
        return vehicles, last_error

    if INVENTORY_CACHE["items"]:
        return INVENTORY_CACHE["items"], last_error or INVENTORY_CACHE["last_error"]
    return [], last_error


def carbarn_vehicle_to_row(v: dict[str, Any]) -> dict[str, Any]:
    make = _pick(v, ["make", "vehicleMake", "manufacturer"])
    model = _pick(v, ["model", "vehicleModel"])
    year = _pick(v, ["year", "manufactureYear", "modelYear"])
    price = _pick(v, ["price", "salePrice", "listingPrice"])
    stock_no = _pick(v, ["stockNo", "stockNO", "stock"])
    stock_candidates = _dedupe_keep_order(
        [
            _pick(v, ["stockNo"]),
            _pick(v, ["stockNO"]),
            _pick(v, ["stock"]),
            _pick(v, ["stockNumber"]),
            _pick(v, ["stockNum"]),
            _pick(v, ["dealerStockId"]),
            _pick(v, ["dealerStockID"]),
            _pick(v, ["stockId"]),
            _pick(v, ["stockID"]),
        ]
    )
    if not _norm_text(stock_no) and stock_candidates:
        stock_no = stock_candidates[0]
    chassis = _pick(v, ["chassisNo", "chassis", "vin"])
    title = _pick(v, ["title", "name"], default="").strip()
    if not title:
        title = " ".join([_norm_text(year), _norm_text(make), _norm_text(model)]).strip()
    if " warranty" in title.lower() or len(title) > 120:
        # Some API titles include full marketing paragraph; keep card title clean.
        title = " ".join([_norm_text(year), _norm_text(make), _norm_text(model)]).strip() or title

    image_url = _pick(
        v,
        [
            "exteriorPhoto",
            "exteriorPhotos",
            "imageUrl",
            "primaryImageUrl",
            "thumbnailUrl",
            "coverImageUrl",
            "mainImage",
            "mainPhoto",
            "heroImage",
        ],
        default="",
    )
    if isinstance(image_url, list) and image_url:
        first_img = image_url[0]
        if isinstance(first_img, dict):
            image_url = _pick(first_img, ["url", "imageUrl", "src", "large", "original"], default="")
        else:
            image_url = str(first_img)
    if isinstance(image_url, dict):
        image_url = _pick(image_url, ["url", "imageUrl", "src", "large", "original"], default="")
    if not image_url:
        exterior = _pick(v, ["exteriorPhoto", "exteriorPhotos"], default=[])
        if isinstance(exterior, list) and exterior:
            first = exterior[0]
            if isinstance(first, dict):
                image_url = _pick(first, ["url", "imageUrl", "src", "large", "original"], default="")
            elif isinstance(first, str):
                image_url = first
    if not image_url:
        interior = _pick(v, ["interiorPhoto", "interiorPhotos"], default=[])
        if isinstance(interior, list) and interior:
            first = interior[0]
            if isinstance(first, dict):
                image_url = _pick(first, ["url", "imageUrl", "src", "large", "original"], default="")
            elif isinstance(first, str):
                image_url = first
    if not image_url:
        auction = _pick(v, ["auctionPhotos", "auctionPhoto"], default=[])
        if isinstance(auction, list) and auction:
            first = auction[0]
            if isinstance(first, dict):
                image_url = _pick(first, ["url", "imageUrl", "src", "large", "original"], default="")
            elif isinstance(first, str):
                image_url = first
    if not image_url:
        photos = _pick(v, ["photos", "images"], default=[])
        if isinstance(photos, list) and photos:
            first = photos[0]
            if isinstance(first, dict):
                image_url = _pick(first, ["url", "imageUrl", "src", "large", "original"], default="")
            elif isinstance(first, str):
                image_url = first

    detail_url = _carbarn_detail_url(v, stock_no)

    return {
        "title": _norm_text(title),
        "year": _norm_text(year),
        "make": _norm_text(make),
        "model": _norm_text(model),
        "price_text": _norm_text(price),
        "price": _norm_price(price),
        "odometer_text": _norm_text(_pick(v, ["odometer", "odometerKm", "kms"])),
        "description": _truncate_text(_pick(v, ["description", "carDescription"]), 220),
        "detail_url": _norm_text(detail_url),
        "image_url": _normalize_image_url(str(image_url)),
        "stock_no": _norm_text(stock_no),
        "stock_candidates": [str(x).strip() for x in stock_candidates if str(x).strip()],
        "chassis": _norm_text(chassis),
        "warranty": build_warranty_data(year, title),
    }


def write_inventory_csv_from_api(force_refresh: bool = False) -> Path:
    items, inv_error = fetch_carbarn_inventory(force=force_refresh)
    if not items:
        raise RuntimeError(f"Carbarn API inventory is empty. {inv_error}".strip())

    rows: list[dict[str, Any]] = []
    for v in items:
        r = carbarn_vehicle_to_row(v)
        if is_published_vehicle(v, r):
            rows.append(r)
    if not rows:
        raise RuntimeError("No published vehicles found in Carbarn API inventory.")
    cache_dir = ROOT / ".ui-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_name = f"inventory_from_api_{int(time.time())}_{next(tempfile._get_candidate_names())}.csv"
    csv_path = cache_dir / tmp_name

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "StockNo",
                "ChassisNo",
                "Make",
                "Model",
                "Year",
                "Price",
                "Odometer",
                "Title",
            ],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "StockNo": r.get("stock_no", ""),
                    "ChassisNo": r.get("chassis", ""),
                    "Make": r.get("make", ""),
                    "Model": r.get("model", ""),
                    "Year": r.get("year", ""),
                    "Price": r.get("price", "") or r.get("price_text", ""),
                    "Odometer": r.get("odometer_text", ""),
                    "Title": r.get("title", ""),
                }
            )
    return csv_path


def build_side_by_side_comparisons(out_dir: str, force_inventory_refresh: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    reg = load_url_registry(out_dir)
    active_urls = reg.get("active_urls", []) if isinstance(reg, dict) else []
    archived_urls = reg.get("archived_urls", []) if isinstance(reg, dict) else []
    submitted_urls_total = len(active_urls) if isinstance(active_urls, list) else 0
    archived_urls_total = len(archived_urls) if isinstance(archived_urls, list) else 0

    manual_rows = load_manual_matches(out_dir)
    if manual_rows:
        comparisons: list[dict[str, Any]] = []
        error_total = 0
        matched_inventory_keys: set[tuple[str, str]] = set()
        identified_url_total = 0

        for item in manual_rows:
            status = str(item.get("status", "not_found")).strip().lower() or "not_found"
            cs = item.get("carsales", {}) if isinstance(item.get("carsales"), dict) else {}
            cb = item.get("carbarn", {}) if isinstance(item.get("carbarn"), dict) else {}
            effective_sold = _is_effectively_sold(cs)
            if status != "error" and _is_effectively_sold(cs):
                status = "sold"

            carsales_row = {
                "title": cs.get("title", "") or cs.get("detail_page_title", ""),
                "year": cs.get("year", ""),
                "make": cs.get("make", ""),
                "model": cs.get("model", ""),
                "price_text": cs.get("price_text", ""),
                "price": cs.get("price", ""),
                "odometer_text": cs.get("odometer_text", ""),
                "description": cs.get("dealer_comments", "") or item.get("match_reason", "") or cs.get("error", ""),
                "detail_url": cs.get("source_url", ""),
                "image_url": cs.get("first_image_url", ""),
                "all_image_urls": cs.get("all_image_urls", []),
                "image_count": cs.get("image_count", ""),
                "stock_no": cs.get("dealer_stock_id", ""),
                "dealer_stock_ids": cs.get("dealer_stock_ids", []),
                "chassis": "",
                "car_code": cs.get("car_code", ""),
                "photo_count": cs.get("photo_count", ""),
                "vin": cs.get("vin", ""),
                "registration_plate": cs.get("registration_plate", ""),
                "body_type": cs.get("body_type", ""),
                "fuel": cs.get("fuel", ""),
                "transmission": cs.get("transmission", ""),
                "dealer_comments": cs.get("dealer_comments", ""),
                "status_label": cs.get("status_label", ""),
                "status_tooltip": cs.get("status_tooltip", ""),
                "price_badge_label": cs.get("price_badge_label", ""),
                "is_sold": bool(effective_sold),
                "match_score": item.get("match_score", ""),
                "match_reason": item.get("match_reason", ""),
                "mismatch_fields": item.get("mismatch_fields", []),
                "mismatch_messages": item.get("mismatch_messages", []),
                "mismatch_count": item.get("mismatch_count", 0),
            }

            if status in {"identified", "sold"}:
                identified_url_total += 1
                carbarn_row = cb
                cb_chassis = _normalize_alnum(cb.get("chassis", ""))
                cb_tails = _carbarn_stock_tails(cb)
                if cb_tails:
                    for cb_stock in cb_tails:
                        matched_inventory_keys.add((cb_stock, cb_chassis))
                elif cb_chassis:
                    matched_inventory_keys.add(("", cb_chassis))
            else:
                if status == "error":
                    error_total += 1
                carbarn_row = {
                    "title": "No confident Carbarn match",
                    "year": "",
                    "make": "",
                    "model": "",
                    "price_text": "",
                    "price": "",
                    "odometer_text": "",
                    "description": item.get("match_reason", ""),
                    "detail_url": "",
                    "image_url": "",
                    "stock_no": "",
                    "chassis": "",
                    "warranty": {
                        "rule_label": "Not Matched",
                        "dealer_warranty": None,
                        "integrity_warranty": None,
                    },
                }
            comparisons.append({"status": status, "carbarn": carbarn_row, "carsales": carsales_row})

        inv_items, inv_error = fetch_carbarn_inventory(force=force_inventory_refresh)
        prepared: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for v in inv_items:
            r = carbarn_vehicle_to_row(v)
            scope = classify_vehicle_scope(v, r)
            prepared.append((scope, v, r))

        carbarn_rows = [r for (scope, _, r) in prepared if scope == "published_scope"]
        carbarn_keys: set[tuple[str, str]] = set()
        for r in carbarn_rows:
            c = _normalize_alnum(r.get("chassis", ""))
            tails = _carbarn_stock_tails(r)
            if tails:
                for s in tails:
                    carbarn_keys.add((s, c))
            elif c:
                carbarn_keys.add(("", c))

        # Drop stale identified rows that no longer map to currently valid (published_scope)
        # inventory. This prevents on-offer/unpublished vehicles from lingering in the dashboard
        # due to old manual_matches.json snapshots.
        stale_identified_skipped = 0
        filtered_comparisons: list[dict[str, Any]] = []
        for comp in comparisons:
            comp_status = str(comp.get("status", "")).strip().lower()
            if comp_status not in {"identified", "sold"}:
                filtered_comparisons.append(comp)
                continue
            cb0 = comp.get("carbarn", {}) if isinstance(comp.get("carbarn"), dict) else {}
            cb_chassis0 = _normalize_alnum(cb0.get("chassis", ""))
            cb_tails0 = _carbarn_stock_tails(cb0)
            if cb_tails0:
                keys0 = [(s, cb_chassis0) for s in cb_tails0]
            elif cb_chassis0:
                keys0 = [("", cb_chassis0)]
            else:
                keys0 = []
            if not keys0 or any(k in carbarn_keys for k in keys0):
                filtered_comparisons.append(comp)
            else:
                stale_identified_skipped += 1
        comparisons = filtered_comparisons

        # Rebuild counters from filtered rows.
        identified_url_total = 0
        error_total = 0
        matched_inventory_keys = set()
        for comp in comparisons:
            comp_status = str(comp.get("status", "")).strip().lower()
            if comp_status in {"identified", "sold"}:
                identified_url_total += 1
                cb = comp.get("carbarn", {}) if isinstance(comp.get("carbarn"), dict) else {}
                cb_chassis = _normalize_alnum(cb.get("chassis", ""))
                cb_tails = _carbarn_stock_tails(cb)
                if cb_tails:
                    for cb_stock in cb_tails:
                        matched_inventory_keys.add((cb_stock, cb_chassis))
                elif cb_chassis:
                    matched_inventory_keys.add(("", cb_chassis))
            elif comp_status == "error":
                error_total += 1

        identified_total = len(carbarn_keys & matched_inventory_keys)
        not_found_total = max(0, len(carbarn_keys) - identified_total)

        # Ensure "Not Found" rows are visible in the explorer even if they were
        # never part of submitted Carsales URLs.
        present_not_found_keys: set[tuple[str, str]] = set()
        for comp in comparisons:
            if str(comp.get("status", "")).lower() != "not_found":
                continue
            cb = comp.get("carbarn", {}) if isinstance(comp.get("carbarn"), dict) else {}
            s = _stock_tail4(cb.get("stock_no", ""))
            c = _normalize_alnum(cb.get("chassis", ""))
            if s or c:
                present_not_found_keys.add((s, c))

        for c in carbarn_rows:
            key = (_stock_tail4(c.get("stock_no", "")), _normalize_alnum(c.get("chassis", "")))
            if not (key[0] or key[1]):
                continue
            if key in matched_inventory_keys or key in present_not_found_keys:
                continue
            present_not_found_keys.add(key)
            carsales_row = {
                "title": "Not found on Carsales",
                "year": c.get("year", ""),
                "make": c.get("make", ""),
                "model": c.get("model", ""),
                "price_text": c.get("price_text", ""),
                "price": "",
                "odometer_text": "",
                "description": "No Carsales URL matched this Carbarn vehicle in the current run.",
                "detail_url": "",
                "image_url": "",
                "all_image_urls": [],
                "image_count": "",
                "stock_no": "",
                "dealer_stock_ids": [],
                "chassis": "",
                "car_code": "",
                "photo_count": "",
                "vin": "",
                "registration_plate": "",
                "body_type": "",
                "fuel": "",
                "transmission": "",
                "match_score": 0.0,
                "match_reason": "no_matching_carsales_url_for_carbarn_vehicle",
                "mismatch_fields": [],
                "mismatch_messages": [],
                "mismatch_count": 0,
                "lookup_urls": _build_carsales_lookup_urls(c.get("year", ""), c.get("make", ""), c.get("model", "")),
            }
            comparisons.append({"status": "not_found", "carbarn": c, "carsales": carsales_row})

        summary = {
            "inventory_total": len(carbarn_rows),
            "inventory_raw_total": len(inv_items),
            "submitted_urls_total": submitted_urls_total,
            "archived_urls_total": archived_urls_total,
            "identified_total": identified_url_total,
            "identified_inventory_total": identified_total,
            "too_many_cards_total": 0,
            "not_found_total": not_found_total,
            "on_offer_total": sum(1 for (scope, _, _) in prepared if scope == "on_offer"),
            "unpublished_total": sum(1 for (scope, _, _) in prepared if scope == "unpublished"),
            "excluded_other_total": error_total,
            "inventory_error": inv_error,
            "inventory_cached_at": INVENTORY_CACHE["cached_at"],
            "published_only": True,
            "manual_mode": True,
            "stale_identified_skipped_total": stale_identified_skipped,
        }
        return comparisons, summary

    # Fallback for older output folders that still only have crawler results.
    legacy_identified = load_identified_cars(out_dir)
    if not legacy_identified:
        summary = {
            "inventory_total": 0,
            "inventory_raw_total": 0,
            "submitted_urls_total": submitted_urls_total,
            "archived_urls_total": archived_urls_total,
            "identified_total": 0,
            "too_many_cards_total": 0,
            "not_found_total": 0,
            "on_offer_total": 0,
            "unpublished_total": 0,
            "excluded_other_total": 0,
            "inventory_error": "",
            "inventory_cached_at": INVENTORY_CACHE["cached_at"],
            "published_only": True,
            "manual_mode": True,
        }
        return [], summary

    inv_items, inv_error = fetch_carbarn_inventory(force=force_inventory_refresh)
    prepared: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for v in inv_items:
        r = carbarn_vehicle_to_row(v)
        scope = classify_vehicle_scope(v, r)
        prepared.append((scope, v, r))

    carbarn_rows = [r for (scope, _, r) in prepared if scope == "published_scope"]
    idx: dict[tuple[str, str, str, int | None], list[dict[str, Any]]] = {}
    for r in legacy_identified:
        key = (
            _norm_key_text(r.get("year", "")),
            _norm_key_text(r.get("make", "")),
            _norm_key_text(r.get("model", "")),
            _norm_price(r.get("price", "") or r.get("price_text", "")),
        )
        idx.setdefault(key, []).append(r)

    comparisons: list[dict[str, Any]] = []
    identified_count = 0
    for scope, _, c in prepared:
        if scope != "published_scope":
            continue
        key = (
            _norm_key_text(c.get("year", "")),
            _norm_key_text(c.get("make", "")),
            _norm_key_text(c.get("model", "")),
            _norm_price(c.get("price", "") or c.get("price_text", "")),
        )
        candidates = idx.get(key, [])
        carsales = candidates[0] if candidates else {
            "title": "Not found on Carsales",
            "year": c.get("year", ""),
            "make": c.get("make", ""),
            "model": c.get("model", ""),
            "price_text": c.get("price_text", ""),
            "price": "",
            "odometer_text": "",
            "description": "No Carsales exact match found for this Carbarn item.",
            "detail_url": "",
            "image_url": "",
            "all_image_urls": [],
            "image_count": "",
            "stock_no": "",
            "dealer_stock_ids": [],
            "chassis": "",
            "car_code": "",
            "photo_count": "",
            "vin": "",
            "registration_plate": "",
            "body_type": "",
            "fuel": "",
            "transmission": "",
            "mismatch_fields": [],
            "mismatch_messages": [],
            "mismatch_count": 0,
            "match_reason": "",
        }
        status = "identified" if candidates else "not_found"
        if candidates:
            identified_count += 1
        comparisons.append({"status": status, "carbarn": c, "carsales": carsales})

    summary = {
        "inventory_total": len(carbarn_rows),
        "inventory_raw_total": len(inv_items),
        "submitted_urls_total": submitted_urls_total,
        "archived_urls_total": archived_urls_total,
        "identified_total": identified_count,
        "too_many_cards_total": 0,
        "not_found_total": max(0, len(carbarn_rows) - identified_count),
        "on_offer_total": sum(1 for (scope, _, _) in prepared if scope == "on_offer"),
        "unpublished_total": sum(1 for (scope, _, _) in prepared if scope == "unpublished"),
        "excluded_other_total": 0,
        "inventory_error": inv_error,
        "inventory_cached_at": INVENTORY_CACHE["cached_at"],
        "published_only": True,
        "manual_mode": False,
    }
    return comparisons, summary


def save_full_comparison_output(out_dir: str, rows: list[dict[str, Any]], summary: dict[str, Any]) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = _full_comparison_file(out)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "rows_count": len(rows),
        "rows": rows,
        "summary": summary,
    }
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


def load_full_comparison_output(out_dir: str) -> dict[str, Any] | None:
    p = _full_comparison_file(Path(out_dir))
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        rows = payload.get("rows", [])
        summary = payload.get("summary", {})
        if not isinstance(rows, list) or not isinstance(summary, dict):
            return None
        return payload
    except Exception:
        return None


def build_and_save_full_comparison_output(out_dir: str, *, force_inventory_refresh: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows, summary = build_side_by_side_comparisons(out_dir, force_inventory_refresh=force_inventory_refresh)
    # Safety normalization for legacy/manual rows:
    # if status_label indicates sold/unavailable, always emit is_sold=true and status=sold.
    for comp in rows:
        if not isinstance(comp, dict):
            continue
        cs = comp.get("carsales", {})
        if not isinstance(cs, dict):
            continue
        sold = _is_effectively_sold(cs)
        cs["is_sold"] = bool(sold)
        if sold and str(comp.get("status", "")).strip().lower() != "error":
            comp["status"] = "sold"
    save_full_comparison_output(out_dir, rows, summary)
    upload_full_comparison_to_pythonanywhere(out_dir)
    return rows, summary


def load_not_found_cars(out_dir: str, inventory_file: str) -> list[dict[str, Any]]:
    inv_path = Path(inventory_file)
    if not inv_path.exists():
        return []

    try:
        if inv_path.suffix.lower() in [".xlsx", ".xls"] or inv_path.name.lower().endswith(".csv.xlsx"):
            inv = pd.read_excel(inv_path)
        else:
            inv = pd.read_csv(inv_path)
    except Exception:
        return []

    cols = {str(c).strip().lower(): c for c in inv.columns}
    make_col = next((c for c in inv.columns if "make" in str(c).strip().lower()), None)
    model_col = next((c for c in inv.columns if "model" in str(c).strip().lower()), None)
    year_col = next((c for c in inv.columns if "year" in str(c).strip().lower()), None)
    price_col = next((c for c in inv.columns if "price" in str(c).strip().lower()), None)
    stock_col = next((c for c in inv.columns if "stockno" in str(c).strip().lower() or "stock no" in str(c).strip().lower()), None)
    chassis_col = next((c for c in inv.columns if "chassis" in str(c).strip().lower()), None)
    if not price_col:
        return []

    def norm(v: Any) -> str:
        s = str(v or "").strip().lower()
        return "" if s == "nan" else s

    def norm_price(v: Any) -> int | None:
        s = "".join(ch for ch in str(v or "") if ch.isdigit())
        return int(s) if s else None

    found_keys: set[tuple[str, str, str, int | None]] = set()
    out_p = Path(out_dir)
    latest = out_p / "my_cars.csv"
    if latest.exists():
        try:
            with latest.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    key = (
                        norm(r.get("inventory_year_filter", "")),
                        norm(r.get("inventory_make_filter", "")),
                        norm(r.get("inventory_model_filter", "")),
                        norm_price(r.get("price", "") or r.get("price_text", "")),
                    )
                    found_keys.add(key)
        except Exception:
            pass

    not_found: list[dict[str, Any]] = []
    for _, row in inv.iterrows():
        key = (
            norm(row.get(year_col, "")) if year_col else "",
            norm(row.get(make_col, "")) if make_col else "",
            norm(row.get(model_col, "")) if model_col else "",
            norm_price(row.get(price_col, "")),
        )
        if key in found_keys:
            continue
        not_found.append(
            {
                "status": "not_found",
                "title": " ".join([str(row.get(year_col, "")).strip() if year_col else "",
                                   str(row.get(make_col, "")).strip() if make_col else "",
                                   str(row.get(model_col, "")).strip() if model_col else ""]).strip(),
                "year": str(row.get(year_col, "")).strip() if year_col else "",
                "make": str(row.get(make_col, "")).strip() if make_col else "",
                "model": str(row.get(model_col, "")).strip() if model_col else "",
                "price_text": str(row.get(price_col, "")).strip(),
                "price": row.get(price_col, ""),
                "odometer_text": "",
                "description": "Not found in current workflow matches (my_cars.csv).",
                "detail_url": "",
                "image_url": "",
                "stock_no": str(row.get(stock_col, "")).strip() if stock_col else "",
                "chassis": str(row.get(chassis_col, "")).strip() if chassis_col else "",
            }
        )
    return not_found


def normalize_carsales_image_url(raw_url: str) -> str:
    """
    Remove only pxc_method=crop from pxcrush URLs.
    """
    try:
        p = urlparse(raw_url)
        host = (p.netloc or "").lower()
        if "pxcrush.net" not in host:
            return raw_url

        pairs = parse_qsl(p.query, keep_blank_values=True)
        filtered = [(k, v) for (k, v) in pairs if not (k == "pxc_method" and str(v).lower() == "crop")]
        new_q = urlencode(filtered, doseq=True)
        return urlunparse((p.scheme, p.netloc, p.path, p.params, new_q, p.fragment))
    except Exception:
        return raw_url


def _sanitize_url_for_request(raw_url: str) -> str:
    s = str(raw_url or "").strip()
    if not s:
        return ""
    try:
        p = urlparse(s)
        if not p.scheme or not p.netloc:
            return s
        path = quote(unquote(p.path), safe="/:@!$&'()*+,;=-._~")
        query = quote(unquote(p.query), safe="=&:@!$'()*+,;/-._~")
        frag = quote(unquote(p.fragment), safe=":@!$&'()*+,;=/-._~")
        return urlunparse((p.scheme, p.netloc, path, p.params, query, frag))
    except Exception:
        return s


def _normalize_alnum(v: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", _norm_text(v)).upper()


def _slugify_carsales(value: Any) -> str:
    s = _norm_text(value).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def _build_carsales_lookup_urls(year: Any, make: Any, model: Any) -> list[dict[str, str]]:
    sy = _slugify_carsales(year)
    smake = _slugify_carsales(make)
    if not sy or not smake:
        return []
    url = f"https://www.carsales.com.au/cars/dealer/{sy}/{smake}/new-south-wales-state/"
    return [{"label": f"NSW dealer {year} {make}", "url": url}]


def _extract_manual_urls(raw_text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in (raw_text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if "carsales.com.au/cars/details/" not in s.lower():
            continue
        if not (s.startswith("http://") or s.startswith("https://")):
            s = f"https://{s.lstrip('/')}"
        s = s.split("?", 1)[0].strip()
        if not s.endswith("/"):
            s += "/"
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _extract_html_text(html: str) -> str:
    txt = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
    txt = re.sub(r"<style\b[^>]*>.*?</style>", " ", txt, flags=re.I | re.S)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = unescape(txt)
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()


def _extract_meta_content(html: str, key: str, *, attr: str = "property") -> str:
    pattern = rf'<meta[^>]+{attr}=["\']{re.escape(key)}["\'][^>]*content=["\']([^"\']+)["\']'
    m = re.search(pattern, html, flags=re.I)
    if m:
        return unescape(m.group(1).strip())
    pattern2 = rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]*{attr}=["\']{re.escape(key)}["\']'
    m2 = re.search(pattern2, html, flags=re.I)
    return unescape(m2.group(1).strip()) if m2 else ""


def _extract_title_like(html: str) -> str:
    for pat in [
        r"<h1[^>]*>(.*?)</h1>",
        r"<title[^>]*>(.*?)</title>",
    ]:
        m = re.search(pat, html, flags=re.I | re.S)
        if m:
            clean = re.sub(r"<[^>]+>", " ", m.group(1))
            clean = unescape(clean)
            clean = re.sub(r"\s+", " ", clean).strip()
            if clean:
                return clean
    return ""


def _extract_int(raw: Any) -> int | None:
    s = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return int(s) if s else None


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        s = str(it or "").strip()
        if not s:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def _extract_carsales_images_from_html(html: str) -> list[str]:
    if not html:
        return []
    # Carsales often embeds image URLs in escaped JSON payloads.
    blob = html.replace("\\/", "/").replace("\\u0026", "&")
    candidates = re.findall(
        r"https?://[^\"'\\s<>]+",
        blob,
        flags=re.I,
    )
    out: list[str] = []
    for raw in candidates:
        u = normalize_carsales_image_url(unescape(raw.strip()))
        low = u.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            continue
        if "pxcrush.net" not in low and "carsales.com.au" not in low:
            continue
        if not any(
            key in low
            for key in [
                "/carsales/car/",
                "/carsales/cars/",
                "pxc_size=",
                "pxc_method=",
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
            ]
        ):
            continue
        if any(bad in low for bad in ["/logo", "/icon", "/sprite", "/avatar", "favicon"]):
            continue
        out.append(u)
    return _dedupe_keep_order(out)


def _extract_carsales_photo_label_count(html: str, text: str) -> int | None:
    if html:
        # Carsales detail page "photo-solid" icon block:
        # <div data-icontype="photo-solid">...</div><span ...>19</span>
        matches = re.findall(
            r'data-icontype=["\']photo-solid["\'][\s\S]{0,2000}?<span[^>]*>\s*([0-9]{1,3})\s*</span>',
            html,
            flags=re.I,
        )
        nums = [int(m) for m in matches if str(m).isdigit()]
        if nums:
            return max(nums)

    if text:
        m = re.search(r"\b([0-9]{1,3})\s+photos?\b", text, flags=re.I)
        if m:
            return int(m.group(1))
    return None


def _extract_first_match(text: str, pattern: str, flags: int = re.I) -> str:
    m = re.search(pattern, text or "", flags=flags)
    return m.group(1).strip() if m else ""


def _extract_labeled_value(text: str, label: str, next_labels: list[str], *, max_len: int = 60) -> str:
    t = str(text or "")
    if not t:
        return ""
    next_alt = "|".join([re.escape(x) for x in next_labels]) if next_labels else "$"
    pat = rf"\b{re.escape(label)}\b\s*[:\-]?\s*(.*?)\s*(?=\b(?:{next_alt})\b|$)"
    m = re.search(pat, t, flags=re.I | re.S)
    if not m:
        return ""
    v = re.sub(r"\s+", " ", m.group(1)).strip()
    if len(v) > max_len:
        v = v[:max_len].strip()
    return v


def _extract_section_text(text: str, start_label: str, end_labels: list[str], *, max_len: int = 4000) -> str:
    t = str(text or "")
    if not t:
        return ""
    end_alt = "|".join([re.escape(x) for x in end_labels]) if end_labels else "$"
    pat = rf"\b{re.escape(start_label)}\b\s*[:\-]?\s*(.*?)\s*(?=\b(?:{end_alt})\b|$)"
    m = re.search(pat, t, flags=re.I | re.S)
    if not m:
        return ""
    v = re.sub(r"\s+", " ", m.group(1)).strip()
    if len(v) > max_len:
        v = v[:max_len].strip()
    return v


def _extract_badge_label_by_icon(html: str, icon_type: str) -> str:
    if not html:
        return ""
    pat = (
        rf'role=["\']button["\'][^>]*>\s*([^<]{{2,120}}?)\s*'
        rf'<div[^>]*data-icontype=["\']{re.escape(icon_type)}["\']'
    )
    m = re.search(pat, html, flags=re.I | re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", unescape(m.group(1))).strip()


def _extract_price_badge_label(html: str) -> str:
    if not html:
        return ""
    # Capture text directly before any Carsales price-* icon.
    m = re.search(
        r'role=["\']button["\'][^>]*>\s*([^<]{2,120}?)\s*<div[^>]*data-icontype=["\'](price-[^"\']+)["\']',
        html,
        flags=re.I | re.S,
    )
    if m:
        return re.sub(r"\s+", " ", unescape(m.group(1))).strip()
    return ""


def _extract_status_label(html: str, text: str) -> str:
    # Prefer strong status phrases.
    t = f"{html or ''} {text or ''}"
    for pat, label in [
        (r"Sold\s*or\s*unavailable", "Sold or unavailable"),
        (r"\bOn\s*hold\b", "On hold"),
    ]:
        if re.search(pat, t, flags=re.I):
            return label
    # Fallback to icon-adjacent badge copy.
    return _extract_badge_label_by_icon(html, "info-solid")


def _extract_on_hold_tooltip(html: str) -> str:
    if not html:
        return ""
    pat = r"On hold[\s\S]{0,2500}?<span[^>]*>\s*The seller has taken the car off the market[\s\S]{0,500}?</span>"
    m = re.search(pat, html, flags=re.I)
    if not m:
        return ""
    tip = re.sub(r"<[^>]+>", " ", m.group(0))
    tip = re.sub(r"\s+", " ", unescape(tip)).strip()
    tip = re.sub(r"^On hold\s*", "", tip, flags=re.I).strip()
    return tip


def _is_effectively_sold(row: dict[str, Any]) -> bool:
    raw = _norm_key_text(row.get("status_label", ""))
    compact = re.sub(r"[^a-z0-9]+", "", raw)
    if compact in {"onhold", "sold", "soldorunavailable", "unavailable"}:
        return True
    if "on hold" in raw or "sold or unavailable" in raw:
        return True
    return bool(row.get("is_sold", False))


def _extract_carsales_price(html: str, text: str) -> int | None:
    if not html and not text:
        return None
    blob = (html or "").replace("\\/", "/").replace("\\u0026", "&")
    txt = text or ""

    # 1) Most reliable: price displayed with Excl. Govt. Charges.
    m = re.search(r"\$([0-9][0-9,]{2,})\s*Excl\.?\s*Govt\.?\s*Charges", txt, flags=re.I)
    if m:
        return _extract_int(m.group(1))

    # 2) JSON-LD offer price.
    m2 = re.search(r'"@type"\s*:\s*"Offer"[\s\S]{0,300}?"price"\s*:\s*"?([0-9]{3,7})"?', blob, flags=re.I)
    if m2:
        return _extract_int(m2.group(1))

    # 3) product:price:amount meta if available.
    m3 = re.search(r'product:price:amount["\'][^>]*content=["\']([0-9][0-9,]{2,})["\']', html or "", flags=re.I)
    if m3:
        return _extract_int(m3.group(1))

    # 4) Fallback to largest reasonable displayed $ value.
    vals = [_extract_int(x) for x in re.findall(r"\$([0-9][0-9,]{2,})", txt)]
    vals = [v for v in vals if v is not None and 500 <= int(v) <= 500000]
    if vals:
        return max(vals)
    return None


def _looks_like_carsales_challenge(text: str, title: str) -> bool:
    t = f"{text or ''} {title or ''}".lower()
    markers = [
        "are you a robot",
        "verify you are human",
        "security check",
        "please verify",
        "captcha",
        "access denied",
        "bot detection",
    ]
    return any(m in t for m in markers)


def _has_meaningful_carsales_details(row: dict[str, Any]) -> bool:
    if str(row.get("error", "")).strip():
        return False
    title = str(row.get("title", "") or "").strip().lower()
    if title in {"", "carsales.com.au"}:
        return False
    if row.get("image_count"):
        return True
    if str(row.get("dealer_stock_id", "")).strip():
        return True
    if str(row.get("car_code", "")).strip():
        return True
    if str(row.get("vin", "")).strip():
        return True
    if row.get("price") is not None:
        return True
    if row.get("odometer_km") is not None:
        return True
    return False


def _extract_year_make_model(headline: str, url: str) -> tuple[str, str, str]:
    year = ""
    make = ""
    model = ""
    h = _norm_text(headline)
    m = re.search(r"\b((?:19|20)\d{2})\b", h)
    if m:
        year = m.group(1)
        tail = h[m.end() :].strip()
        if tail:
            toks = [t for t in re.split(r"\s+", tail) if t]
            if toks:
                make = toks[0]
                model = " ".join(toks[1:]).strip()
    if not year:
        m_url = re.search(r"/cars/details/((?:19|20)\d{2})-", url, flags=re.I)
        if m_url:
            year = m_url.group(1)
    if not make or not model:
        slug_match = re.search(r"/cars/details/([^/]+)/", url, flags=re.I)
        if slug_match:
            slug = slug_match.group(1)
            slug = re.sub(r"^(?:19|20)\d{2}-", "", slug)
            tokens = [t for t in slug.split("-") if t and t.lower() not in {"auto", "manual"}]
            if tokens:
                if not make:
                    make = tokens[0].title()
                if not model and len(tokens) > 1:
                    model = " ".join(tokens[1:]).strip().title()
    return year, make, model


def _fetch_html_with_playwright(url: str) -> tuple[str, str]:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as e:
        return "", f"playwright_import_failed: {e}"

    challenge_markers = [
        "are you a robot",
        "verify you are human",
        "security check",
        "please verify",
        "captcha",
        "challenge",
        "access denied",
    ]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="en-AU",
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=70000)
            page.wait_for_timeout(2200)
            html = page.content()
            text = page.inner_text("body") if page.locator("body").count() else ""
            low = (text or "").lower()
            if any(m in low for m in challenge_markers):
                # Give dynamic challenge pages more time before failing.
                page.wait_for_timeout(4000)
                html = page.content()
            context.close()
            browser.close()
            return html, ""
    except Exception as e:
        return "", f"playwright_fetch_failed: {e}"


def _parse_carsales_listing_from_html(url: str, html: str) -> dict[str, Any]:
    base = {
        "source_url": url,
        "network_id": "",
        "title": "",
        "detail_page_title": "",
        "year": "",
        "make": "",
        "model": "",
        "price_text": "",
        "price": None,
        "odometer_text": "",
        "odometer_km": None,
        "dealer_stock_id": "",
        "dealer_stock_ids": [],
        "car_code": "",
        "photo_count": None,
        "first_image_url": "",
        "all_image_urls": [],
        "image_count": 0,
        "vin": "",
        "registration_plate": "",
        "body_type": "",
        "fuel": "",
        "transmission": "",
        "dealer_comments": "",
        "status_label": "",
        "status_tooltip": "",
        "price_badge_label": "",
        "is_sold": False,
        "section_comments_from_dealer": False,
        "section_car_details": False,
        "section_overview": False,
        "section_specifications": False,
        "section_vehicle_history_report": False,
        "section_about_this_dealer": False,
        "error": "",
    }
    m_id = re.search(r"(OAG-AD-\d+)", url, flags=re.I)
    if m_id:
        base["network_id"] = m_id.group(1).upper()
    if not html:
        base["error"] = "empty_html"
        return base

    text = _extract_html_text(html)
    title = _extract_meta_content(html, "og:title")
    if not title:
        title = _extract_title_like(html)
    year, make, model = _extract_year_make_model(title, url)

    price = _extract_carsales_price(html, text)
    price_text = f"${price:,}" if price is not None else ""

    odometer_text = _extract_labeled_value(
        text,
        "Odometer",
        ["Body type", "Fuel", "Transmission", "Comments", "Dealer stock ID", "VIN", "Registration plate"],
        max_len=24,
    ).replace(" ", "")
    odometer_km = _extract_int(odometer_text)

    stock_matches = re.findall(r"Dealer\s+stock\s+ID\s*[:\-]?\s*([A-Za-z0-9\-]+)", text, flags=re.I)
    dealer_stock_ids = _dedupe_keep_order([s.strip() for s in stock_matches])
    dealer_stock_id = dealer_stock_ids[0] if dealer_stock_ids else ""
    car_code = _extract_first_match(text, r"Car\s+code\s*[:\-]?\s*([A-Za-z0-9\-]+)")
    vin = _extract_labeled_value(
        text,
        "VIN",
        ["Build date", "Compliance date", "Dealer stock ID", "Vehicle history report", "Registration plate"],
        max_len=24,
    )
    registration_plate = _extract_labeled_value(
        text,
        "Registration plate",
        ["VIN", "Build date", "Compliance date", "Dealer stock ID"],
        max_len=20,
    )
    body_type = _extract_labeled_value(
        text,
        "Body type",
        ["Transmission", "Engine", "ANCAP", "Registration plate", "VIN", "Fuel"],
        max_len=35,
    )
    fuel = _extract_labeled_value(
        text,
        "Fuel",
        ["Transmission", "Comments", "Body type", "Odometer", "Dealer stock ID", "VIN"],
        max_len=20,
    )
    transmission = _extract_labeled_value(
        text,
        "Transmission",
        ["Comments", "Engine", "Dealer stock ID", "Body type", "Fuel", "Odometer", "VIN"],
        max_len=24,
    )
    dealer_comments = _extract_section_text(
        text,
        "Comments from the dealer",
        [
            "Car details",
            "Overview",
            "Specifications",
            "Vehicle history report",
            "About this dealer",
            "Get in touch with the dealer",
        ],
        max_len=5000,
    )
    status_label = _extract_status_label(html, text)
    status_tooltip = _extract_on_hold_tooltip(html)
    price_badge_label = _extract_price_badge_label(html)
    section_comments_from_dealer = bool(re.search(r"\bComments from the dealer\b", text, flags=re.I))
    section_car_details = bool(re.search(r"\bCar details\b", text, flags=re.I))
    section_overview = bool(re.search(r"\bOverview\b", text, flags=re.I))
    section_specifications = bool(re.search(r"\bSpecifications\b", text, flags=re.I))
    section_vehicle_history_report = bool(re.search(r"\bVehicle history report\b", text, flags=re.I))
    section_about_this_dealer = bool(re.search(r"\bAbout this dealer\b", text, flags=re.I))
    is_sold = _norm_key_text(status_label) in {"onhold", "soldorunavailable", "sold", "unavailable"}

    photo_count = _extract_carsales_photo_label_count(html, text)

    image_candidates = _extract_carsales_images_from_html(html)
    og_image = normalize_carsales_image_url(_extract_meta_content(html, "og:image"))
    if og_image:
        image_candidates = _dedupe_keep_order([og_image, *image_candidates])
    image_url = image_candidates[0] if image_candidates else ""

    base.update(
        {
            "title": title or "",
            "detail_page_title": _extract_title_like(html),
            "year": year,
            "make": make,
            "model": model,
            "price_text": price_text,
            "price": price,
            "odometer_text": odometer_text,
            "odometer_km": odometer_km,
            "dealer_stock_id": dealer_stock_id,
            "dealer_stock_ids": dealer_stock_ids,
            "car_code": car_code,
            "photo_count": photo_count,
            "first_image_url": image_url,
            "all_image_urls": image_candidates,
            "image_count": len(image_candidates),
            "vin": vin,
            "registration_plate": registration_plate,
            "body_type": body_type,
            "fuel": fuel,
            "transmission": transmission,
            "dealer_comments": dealer_comments,
            "status_label": status_label,
            "status_tooltip": status_tooltip,
            "price_badge_label": price_badge_label,
            "is_sold": is_sold,
            "section_comments_from_dealer": section_comments_from_dealer,
            "section_car_details": section_car_details,
            "section_overview": section_overview,
            "section_specifications": section_specifications,
            "section_vehicle_history_report": section_vehicle_history_report,
            "section_about_this_dealer": section_about_this_dealer,
        }
    )
    if _looks_like_carsales_challenge(text, title):
        base["error"] = "browser_verification_incomplete: challenge_page_detected"
    elif not _has_meaningful_carsales_details(base):
        base["error"] = "browser_verification_incomplete: listing_details_missing"
    return base


def fetch_carsales_listing(url: str) -> dict[str, Any]:
    html = ""
    fetch_err = ""
    try:
        req = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-AU,en;q=0.9",
                "Referer": "https://www.carsales.com.au/",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        with urlopen(req, timeout=35) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        fetch_err = f"fetch_failed: {e}"

    if not html and ("403" in fetch_err or "forbidden" in fetch_err.lower()):
        html, pw_err = _fetch_html_with_playwright(url)
        if not html and pw_err:
            failed = _parse_carsales_listing_from_html(url, "")
            failed["error"] = f"{fetch_err}; {pw_err}"
            return failed
    elif not html:
        failed = _parse_carsales_listing_from_html(url, "")
        failed["error"] = fetch_err or "fetch_failed"
        return failed

    return _parse_carsales_listing_from_html(url, html)


def _model_similarity(a: str, b: str) -> float:
    ta = {x for x in re.split(r"[^a-z0-9]+", a.lower()) if x}
    tb = {x for x in re.split(r"[^a-z0-9]+", b.lower()) if x}
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if inter == 0:
        return 0.0
    return (2.0 * inter) / (len(ta) + len(tb))


def _stock_tail4(v: Any) -> str:
    digits = "".join(ch for ch in str(v or "") if ch.isdigit())
    if not digits:
        return ""
    return digits[-4:] if len(digits) >= 4 else digits


def _stock_tail3(v: Any) -> str:
    digits = "".join(ch for ch in str(v or "") if ch.isdigit())
    if not digits:
        return ""
    return digits[-3:] if len(digits) >= 3 else digits


def _tail4_set(values: list[Any]) -> set[str]:
    out: set[str] = set()
    for v in values:
        t = _stock_tail4(v)
        if t:
            out.add(t)
    return out


def _tail3_set(values: list[Any]) -> set[str]:
    out: set[str] = set()
    for v in values:
        t = _stock_tail3(v)
        if t:
            out.add(t)
    return out


def _carsales_stock_tails(carsales_row: dict[str, Any]) -> set[str]:
    vals: list[Any] = [carsales_row.get("dealer_stock_id", ""), carsales_row.get("stock_no", "")]
    ds = carsales_row.get("dealer_stock_ids", [])
    if isinstance(ds, list):
        vals.extend(ds)
    return _tail4_set(vals)


def _carsales_stock_tail3s(carsales_row: dict[str, Any]) -> set[str]:
    vals: list[Any] = [carsales_row.get("dealer_stock_id", ""), carsales_row.get("stock_no", "")]
    ds = carsales_row.get("dealer_stock_ids", [])
    if isinstance(ds, list):
        vals.extend(ds)
    return _tail3_set(vals)


def _carbarn_stock_tails(row: dict[str, Any]) -> set[str]:
    vals: list[Any] = [row.get("stock_no", "")]
    sc = row.get("stock_candidates", [])
    if isinstance(sc, list):
        vals.extend(sc)
    return _tail4_set(vals)


def _carbarn_stock_tail3s(row: dict[str, Any]) -> set[str]:
    vals: list[Any] = [row.get("stock_no", "")]
    sc = row.get("stock_candidates", [])
    if isinstance(sc, list):
        vals.extend(sc)
    return _tail3_set(vals)


def _match_to_carbarn_inventory(carsales_row: dict[str, Any], inventory_rows: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float, str]:
    # STRICT RULE: stock-id is the only initial matcher.
    # Carsales full stock may be e.g. 19981058 while Carbarn keeps last 4 digits (1058).
    cs_tails = _carsales_stock_tails(carsales_row)
    cs_tail3s = _carsales_stock_tail3s(carsales_row)
    if not cs_tails and not cs_tail3s:
        return None, 0.0, "missing_carsales_stock_id"

    # Priority 1: last-4 stock match
    for r in inventory_rows:
        inv_tails = _carbarn_stock_tails(r)
        hit = sorted(cs_tails & inv_tails)
        if hit:
            return r, 100.0, f"stock_tail4_match:{hit[0]}"

    # Priority 2: fallback last-3 stock match
    for r in inventory_rows:
        inv_tail3s = _carbarn_stock_tail3s(r)
        hit3 = sorted(cs_tail3s & inv_tail3s)
        if hit3:
            return r, 95.0, f"stock_tail3_match:{hit3[0]}"

    return None, 0.0, (
        f"stock_tail4_not_found:{','.join(sorted(cs_tails))};"
        f"stock_tail3_not_found:{','.join(sorted(cs_tail3s))}"
    )


def _build_mismatch_report(carsales_row: dict[str, Any], carbarn_row: dict[str, Any]) -> tuple[list[str], list[str]]:
    fields: list[str] = []
    messages: list[str] = []

    cs_price = _extract_int(carsales_row.get("price", "") or carsales_row.get("price_text", ""))
    cb_price = _extract_int(carbarn_row.get("price", "") or carbarn_row.get("price_text", ""))
    if cs_price is not None and cb_price is not None and cs_price != cb_price:
        fields.append("price")
        messages.append(f"Price mismatch: Carsales ${cs_price:,} vs Carbarn ${cb_price:,}")

    cs_odo = _extract_int(carsales_row.get("odometer_km", "") or carsales_row.get("odometer_text", ""))
    cb_odo = _extract_int(carbarn_row.get("odometer_text", ""))
    if cs_odo is not None and cb_odo is not None and cs_odo != cb_odo:
        fields.append("odometer")
        messages.append(f"Odometer mismatch: Carsales {cs_odo:,}km vs Carbarn {cb_odo:,}km")

    cs_year = _norm_key_text(carsales_row.get("year", ""))
    cb_year = _norm_key_text(carbarn_row.get("year", ""))
    if cs_year and cb_year and cs_year != cb_year:
        fields.append("year")
        messages.append(f"Year mismatch: Carsales {carsales_row.get('year', '')} vs Carbarn {carbarn_row.get('year', '')}")

    cs_make = _norm_key_text(carsales_row.get("make", ""))
    cb_make = _norm_key_text(carbarn_row.get("make", ""))
    if cs_make and cb_make and cs_make != cb_make:
        fields.append("make")
        messages.append(f"Make mismatch: Carsales {carsales_row.get('make', '')} vs Carbarn {carbarn_row.get('make', '')}")

    # Business rule: ignore model mismatch (Carsales model strings are often more verbose).

    return fields, messages


def _manual_urls_file(out_dir: Path) -> Path:
    primary = out_dir / CARSALES_URLS_FILENAME
    if primary.exists():
        return primary
    legacy = out_dir / MANUAL_URLS_FILENAME
    if legacy.exists():
        return legacy
    return primary


def _url_registry_file(out_dir: Path) -> Path:
    return out_dir / CARSALES_URL_REGISTRY_FILENAME


def _manual_matches_file(out_dir: Path) -> Path:
    return out_dir / MANUAL_MATCHES_FILENAME


def _manual_progress_file(out_dir: Path) -> Path:
    return out_dir / MANUAL_PROGRESS_FILENAME


def _full_comparison_file(out_dir: Path) -> Path:
    return out_dir / FULL_COMPARISON_FILENAME


def backup_manual_matches_file(out_dir: str, *, keep_latest: int = 20) -> tuple[bool, str]:
    out = Path(out_dir).resolve()
    src = _manual_matches_file(out)
    if not src.exists():
        return False, "manual_matches_missing"
    backup_dir = out / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = backup_dir / f"manual_matches_{stamp}.json"
    try:
        shutil.copy2(src, dst)
        # Rotation: keep only latest N backups.
        files = sorted(backup_dir.glob("manual_matches_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep_latest:]:
            try:
                old.unlink()
            except Exception:
                pass
        return True, str(dst)
    except Exception as e:
        return False, f"backup_error:{e}"


def upload_full_comparison_to_pythonanywhere(out_dir: str) -> tuple[bool, str]:
    username = PYTHONANYWHERE_USERNAME
    token = PYTHONANYWHERE_TOKEN
    host = PYTHONANYWHERE_HOST
    if not username or not token:
        return False, "pythonanywhere_credentials_missing"

    local_path = _full_comparison_file(Path(out_dir).resolve())
    if not local_path.exists():
        return False, f"file_not_found:{local_path}"

    remote_dir = PYTHONANYWHERE_REMOTE_DIR
    if not remote_dir and username.lower() == "carbarnau":
        remote_dir = "/home/carbarnau/workflow-test/apps/carsale_scraper"
    if not remote_dir:
        remote_dir = f"/home/{username}/find_my_cars_output"
    remote_dir = "/" + str(remote_dir).strip().strip("/")
    remote_path = f"{remote_dir}/{FULL_COMPARISON_FILENAME}"
    endpoint = f"https://{host}/api/v0/user/{username}/files/path{remote_path}"
    headers = {"Authorization": f"Token {token}"}

    # Preferred path: requests, when available.
    if requests is not None:
        try:
            with local_path.open("rb") as f:
                resp = requests.post(
                    endpoint,
                    headers=headers,
                    files={"content": (local_path.name, f, "application/json")},
                    timeout=45,
                )
            if resp.status_code in (200, 201):
                return True, f"uploaded:{remote_path}"
            return False, f"upload_failed:{resp.status_code}:{resp.text[:300]}"
        except Exception as e:
            return False, f"upload_error:{e}"

    # Fallback path: urllib multipart upload (no external dependency).
    try:
        boundary = f"----WebKitFormBoundary{int(time.time() * 1000)}"
        with local_path.open("rb") as f:
            file_bytes = f.read()
        pre = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="content"; filename="{local_path.name}"\r\n'
            f"Content-Type: application/json\r\n\r\n"
        ).encode("utf-8")
        post = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body = pre + file_bytes + post
        req_headers = {
            **headers,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        }
        req = Request(endpoint, data=body, headers=req_headers, method="POST")
        with urlopen(req, timeout=45) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            raw = resp.read() or b""
            text = raw.decode("utf-8", errors="ignore")
        if code in (200, 201):
            return True, f"uploaded:{remote_path}"
        return False, f"upload_failed:{code}:{text[:300]}"
    except Exception as e:
        return False, f"upload_error:{e}"


def _norm_url_key(url: str) -> str:
    return str(url or "").strip().lower()


def _parse_stock_id_tokens(raw: str) -> list[str]:
    text = str(raw or "")
    if not text.strip():
        return []
    seen: set[str] = set()
    tokens: list[str] = []
    for part in re.split(r"[\s,;|]+", text):
        tok = re.sub(r"[^\w-]", "", part.strip())
        if not tok:
            continue
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        tokens.append(tok)
    return tokens


def _extract_row_stock_ids(row: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    if not isinstance(row, dict):
        return out
    carbarn = row.get("carbarn", {}) if isinstance(row.get("carbarn", {}), dict) else {}
    carsales = row.get("carsales", {}) if isinstance(row.get("carsales", {}), dict) else {}
    values: list[Any] = [
        row.get("matched_stock_no", ""),
        carbarn.get("stock_no", ""),
        carsales.get("stock_no", ""),
        carsales.get("dealer_stock_id", ""),
    ]
    dealer_stock_ids = carsales.get("dealer_stock_ids", [])
    if isinstance(dealer_stock_ids, list):
        values.extend(dealer_stock_ids)
    for raw in values:
        s = str(raw or "").strip()
        if not s:
            continue
        out.add(s.lower())
        digits = re.sub(r"\D", "", s)
        if digits:
            out.add(digits.lower())
    return out


def load_manual_progress(out_dir: str) -> dict[str, Any]:
    path = _manual_progress_file(Path(out_dir))
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def save_manual_progress(
    out_dir: str,
    *,
    urls_total: int,
    urls_done: int,
    last_processed_url: str,
    recycle_every: int,
    throttle_min_seconds: float,
    throttle_max_seconds: float,
) -> None:
    path = _manual_progress_file(Path(out_dir))
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "urls_total": int(max(0, urls_total)),
        "urls_done": int(max(0, urls_done)),
        "last_processed_url": str(last_processed_url or ""),
        "recycle_every": int(max(1, recycle_every)),
        "throttle_min_seconds": float(max(0.0, throttle_min_seconds)),
        "throttle_max_seconds": float(max(0.0, throttle_max_seconds)),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _reset_antibot_browser() -> None:
    _save_antibot_storage_state(reason="browser_reset", min_interval_seconds=0.0)
    with ANTI_BOT_BROWSER_LOCK:
        page = ANTI_BOT_BROWSER.get("page")
        context = ANTI_BOT_BROWSER.get("context")
        browser = ANTI_BOT_BROWSER.get("browser")
        pw = ANTI_BOT_BROWSER.get("playwright")
        ANTI_BOT_BROWSER["page"] = None
        ANTI_BOT_BROWSER["context"] = None
        ANTI_BOT_BROWSER["browser"] = None
        ANTI_BOT_BROWSER["playwright"] = None
    for obj, meth in [(page, "close"), (context, "close"), (browser, "close"), (pw, "stop")]:
        try:
            if obj is not None:
                getattr(obj, meth)()
        except Exception:
            pass


def _ensure_antibot_browser_page() -> tuple[Any, str]:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as e:
        return None, f"playwright_import_failed: {e}"

    with ANTI_BOT_BROWSER_LOCK:
        page = ANTI_BOT_BROWSER.get("page")
        if page is not None:
            # If user manually closed the anti-bot window, stale objects remain.
            # Probe safely and rebuild session when disconnected.
            try:
                _ = page.url
                return page, ""
            except Exception:
                pass
            ANTI_BOT_BROWSER["page"] = None
            ANTI_BOT_BROWSER["context"] = None
            ANTI_BOT_BROWSER["browser"] = None
            ANTI_BOT_BROWSER["playwright"] = None
        try:
            with ANTI_BOT_SESSION_LOCK:
                storage_state_path = str(ANTI_BOT_SESSION.get("storage_state_path") or "").strip()
            state_file = Path(storage_state_path) if storage_state_path else None
            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=False,
                args=[
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
    ],
            )
            context_kwargs: dict[str, Any] = {
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "locale": "en-AU",
                "viewport": {"width": 1360, "height": 900},
            }
            if state_file is not None and state_file.exists():
                context_kwargs["storage_state"] = str(state_file)
                STATE.append_log(f"[session] loading storage state: {state_file}")
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            ANTI_BOT_BROWSER["playwright"] = pw
            ANTI_BOT_BROWSER["browser"] = browser
            ANTI_BOT_BROWSER["context"] = context
            ANTI_BOT_BROWSER["page"] = page
            return page, ""
        except Exception as e:
            return None, f"antibot_browser_start_failed: {e}"


def _open_url_in_antibot_browser(url: str) -> tuple[bool, str]:
    target = str(url or "").strip()
    if not target:
        return False, "empty_url"
    page, err = _ensure_antibot_browser_page()
    if page is None:
        return False, err
    try:
        page.goto(target, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(1200)
        solved, solved_msg = _try_solve_slider_if_present(page)
        if solved:
            STATE.append_log(f"[anti-bot slider] {solved_msg}")
        page.bring_to_front()
        return True, ""
    except Exception as e:
        msg = str(e)
        if "cannot switch to a different thread" in msg.lower() or "has exited" in msg.lower():
            _reset_antibot_browser()
            page2, err2 = _ensure_antibot_browser_page()
            if page2 is None:
                return False, f"antibot_browser_open_failed: {err2 or msg}"
            try:
                page2.goto(target, wait_until="domcontentloaded", timeout=90000)
                page2.wait_for_timeout(1200)
                solved, solved_msg = _try_solve_slider_if_present(page2)
                if solved:
                    STATE.append_log(f"[anti-bot slider] {solved_msg}")
                page2.bring_to_front()
                return True, ""
            except Exception as e2:
                return False, f"antibot_browser_open_failed: {e2}"
        return False, f"antibot_browser_open_failed: {e}"


def _get_antibot_page_html() -> tuple[str, str]:
    page, err = _ensure_antibot_browser_page()
    if page is None:
        return "", err
    try:
        return page.content() or "", ""
    except Exception as e:
        return "", f"antibot_page_content_failed: {e}"


def _wait_for_antibot_listing_details(
    url: str,
    *,
    timeout_seconds: int = 120,
    poll_seconds: float = 2.0,
    stop_checker: Any = None,
) -> tuple[dict[str, Any], str]:
    started = time.time()
    last_err = ""
    next_slider_try_at = 0.0
    saw_challenge = False
    saved_after_challenge = False
    slider_solved = False

    # Helper to quickly check if any challenge is present (without heavy solver)
    def _is_challenge_present(page: Any) -> bool:
        if page is None:
            return False
        try:
            # Quick‑visible‑element checks (these MUST be visible)
            # Check for the main DataDome captcha container or slider elements
            structural = [
                "ddv1-captcha-container",
                "captcha__frame",
                "slidercontainer",
                "slidertarget",
                ".geetest_slider",
                ".geetest_btn",
                ".nc_iconfont.btn_slide",
            ]
            # Try to see if any known structural element is visible on the main page
            for selector in structural:
                try:
                    loc = page.locator(selector)
                    if loc.count() > 0 and loc.first.is_visible():
                        return True
                except Exception:
                    continue

            # Check iframes – captcha often lives inside a cross‑domain iframe
            for fr in getattr(page, "frames", []):
                try:
                    furl = str(getattr(fr, "url", "") or "").lower()
                    if "captcha-delivery.com/captcha" in furl or "datadome" in furl:
                        return True
                    # Also check for structural selectors inside the frame
                    for selector in structural:
                        try:
                            loc = fr.locator(selector)
                            if loc.count() > 0 and loc.first.is_visible():
                                return True
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception:
            # If we can't read the page at all, assume a challenge might be present
            return True
        return False

    while (time.time() - started) <= max(5, timeout_seconds):
        if callable(stop_checker):
            try:
                if bool(stop_checker()):
                    return _parse_carsales_listing_from_html(url, ""), "run_stopped"
            except Exception:
                pass

        now_ts = time.time()

        # Only try to solve if we haven't already solved, and the cooldown has passed
        if not slider_solved and now_ts >= next_slider_try_at:
            page, page_err = _ensure_antibot_browser_page()
            if page is not None:
                # Fast check: is a challenge actually visible?
                if not _is_challenge_present(page):
                    # No challenge – treat as solved (either never existed or cleared manually)
                    slider_solved = True
                    if not saved_after_challenge:
                        saved, _ = _save_antibot_storage_state(reason="no_challenge_fast", min_interval_seconds=0.0)
                        saved_after_challenge = True
                    STATE.append_log("[anti-bot] no challenge detected, continuing.")
                else:
                    # Challenge present – attempt to solve
                    solved, solve_msg = _try_solve_slider_if_present(page)
                    if solved:
                        STATE.append_log("[anti-bot slider] solved during verification wait")
                        slider_solved = True
                        saved_after_challenge = True
                        next_slider_try_at = now_ts + 2.5
                    else:
                        # Solver failed, but maybe challenge cleared in the meantime? Re-check after a short delay.
                        if _is_challenge_present(page):
                            # Still there – schedule another attempt
                            if solve_msg and solve_msg not in {"no_challenge_marker_detected", "slider_not_solved"}:
                                last_err = solve_msg
                            next_slider_try_at = now_ts + 1.1
                        else:
                            # Challenge cleared despite solver returning False (e.g., manual solve)
                            slider_solved = True
                            if not saved_after_challenge:
                                saved, _ = _save_antibot_storage_state(reason="challenge_cleared_fast", min_interval_seconds=0.0)
                                saved_after_challenge = True
            elif page_err:
                last_err = page_err
                next_slider_try_at = now_ts + 1.5

        html, err = _get_antibot_page_html()
        if html:
            # If the page still contains the word "captcha" (e.g. "Captcha verification passed"), we note it
            # but do NOT reset slider_solved. The structural challenge elements are gone, so we rely on
            # the slider_solved flag to stay True. If a genuinely new challenge appears later, the structural
            # detection in the solver branch would pick it up.
            if _looks_like_carsales_challenge(html, ""):
                saw_challenge = True

            parsed = _parse_carsales_listing_from_html(url, html)
            if _has_meaningful_carsales_details(parsed):
                if not saved_after_challenge:
                    reason = "challenge_cleared" if saw_challenge else "listing_details_ready"
                    saved, _ = _save_antibot_storage_state(reason=reason, min_interval_seconds=0.0)
                    if saved:
                        saved_after_challenge = True
                return parsed, ""
            last_err = parsed.get("error", "") or "details_missing"
        elif err:
            last_err = err

        time.sleep(max(0.5, poll_seconds))

    return _parse_carsales_listing_from_html(url, ""), f"browser_verification_timeout: {last_err or 'details_not_available'}"


def _is_stop_requested() -> bool:
    with STATE.lock:
        return bool(STATE.stop_requested)


def _sleep_interruptible(seconds: float, step: float = 0.2) -> bool:
    remaining = max(0.0, float(seconds or 0.0))
    while remaining > 0:
        if _is_stop_requested():
            return False
        chunk = min(step, remaining)
        time.sleep(chunk)
        remaining -= chunk
    return True

def _try_solve_slider_if_present(page: Any) -> tuple[bool, str]:
    # -------- Strict structural check only --------
    challenge_present = False
    try:
        structural = [
            "ddv1-captcha-container",
            "captcha__frame",
            "slidercontainer",
            "slidertarget",
            ".geetest_slider",
            ".geetest_btn",
            ".nc_iconfont.btn_slide",
        ]
        # Main page
        for selector in structural:
            try:
                loc = page.locator(selector)
                if loc.count() > 0 and loc.first.is_visible():
                    challenge_present = True
                    break
            except Exception:
                continue
        if not challenge_present:
            # Check iframes
            for fr in getattr(page, "frames", []):
                try:
                    furl = str(getattr(fr, "url", "") or "").lower()
                    if "captcha-delivery.com/captcha" in furl or "datadome" in furl:
                        challenge_present = True
                        break
                    for selector in structural:
                        try:
                            loc = fr.locator(selector)
                            if loc.count() > 0 and loc.first.is_visible():
                                challenge_present = True
                                break
                        except Exception:
                            continue
                    if challenge_present:
                        break
                except Exception:
                    continue
    except Exception:
        challenge_present = True

    if not challenge_present:
        return False, "no_challenge_detected"

    # -------- Challenge exists – attempt to solve --------
    for button_sel, track_sel in SLIDER_SELECTOR_CANDIDATES:
        try:
            ok = solve_slider(
                page=page,
                slider_button_selector=button_sel,
                slider_track_selector=track_sel,
                max_attempts=2,
            )
            if ok:
                page.wait_for_timeout(1400)
                _save_antibot_storage_state(reason=f"slider_solved:{button_sel}", min_interval_seconds=0.0)
                return True, f"solved_with:{button_sel}::{track_sel}"
        except Exception:
            continue

    return False, "slider_not_solved"


def load_url_registry(out_dir: str) -> dict[str, Any]:
    out_path = Path(out_dir)
    reg_path = _url_registry_file(out_path)
    if reg_path.exists():
        try:
            payload = json.loads(reg_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                active = payload.get("active_urls", [])
                archived = payload.get("archived_urls", [])
                if isinstance(active, list) and isinstance(archived, list):
                    return payload
        except Exception:
            pass

    legacy_urls: list[str] = []
    old_path = _manual_urls_file(out_path)
    if old_path.exists():
        try:
            payload = json.loads(old_path.read_text(encoding="utf-8"))
            urls = payload.get("urls", []) if isinstance(payload, dict) else []
            if isinstance(urls, list):
                legacy_urls = [str(u).strip() for u in urls if str(u).strip()]
        except Exception:
            legacy_urls = []

    now_iso = datetime.now().isoformat(timespec="seconds")
    known = [{"url": u, "state": "active", "first_seen_at": now_iso, "last_seen_at": now_iso} for u in legacy_urls]
    return {
        "updated_at": now_iso,
        "active_urls": legacy_urls,
        "archived_urls": [],
        "known_urls": known,
    }


def load_manual_urls(out_dir: str, *, include_archived: bool = False) -> list[str]:
    payload = load_url_registry(out_dir)
    active = payload.get("active_urls", [])
    archived = payload.get("archived_urls", [])
    out: list[str] = []
    if isinstance(active, list):
        out.extend([str(u).strip() for u in active if str(u).strip()])
    if include_archived and isinstance(archived, list):
        out.extend([str(u).strip() for u in archived if str(u).strip()])
    seen: set[str] = set()
    uniq: list[str] = []
    for u in out:
        k = u.lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(u)
    return uniq


def save_manual_urls(out_dir: str, urls: list[str]) -> Path:
    p = Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)
    target = _url_registry_file(p)
    existing = load_url_registry(out_dir)
    old_active = existing.get("active_urls", []) if isinstance(existing, dict) else []
    old_archived = existing.get("archived_urls", []) if isinstance(existing, dict) else []
    known_rows = existing.get("known_urls", []) if isinstance(existing, dict) else []
    by_key: dict[str, dict[str, Any]] = {}
    now_iso = datetime.now().isoformat(timespec="seconds")

    for row in known_rows if isinstance(known_rows, list) else []:
        if not isinstance(row, dict):
            continue
        u = str(row.get("url", "")).strip()
        if not u:
            continue
        by_key[u.lower()] = dict(row)

    old_active_set = {str(u).strip().lower() for u in old_active if str(u).strip()}
    new_active_set = {str(u).strip().lower() for u in urls if str(u).strip()}
    new_urls = [str(u).strip() for u in urls if str(u).strip()]
    archived_urls: list[str] = []
    archived_keys: set[str] = set()

    for u in old_archived if isinstance(old_archived, list) else []:
        s = str(u).strip()
        if not s:
            continue
        k = s.lower()
        if k in new_active_set or k in archived_keys:
            continue
        archived_urls.append(s)
        archived_keys.add(k)

    for k in sorted(old_active_set - new_active_set):
        old_u = next((str(u).strip() for u in old_active if str(u).strip().lower() == k), k)
        if k not in archived_keys:
            archived_urls.append(old_u)
            archived_keys.add(k)

    for u in new_urls:
        k = u.lower()
        row = by_key.get(k, {"url": u, "first_seen_at": now_iso})
        row["url"] = u
        row["state"] = "active"
        row["last_seen_at"] = now_iso
        by_key[k] = row

    for u in archived_urls:
        k = u.lower()
        row = by_key.get(k, {"url": u, "first_seen_at": now_iso})
        row["url"] = u
        row["state"] = "archived"
        row["last_seen_at"] = now_iso
        by_key[k] = row

    payload = {
        "updated_at": now_iso,
        "count": len(new_urls),
        "active_urls": new_urls,
        "archived_urls": archived_urls,
        "known_urls": list(by_key.values()),
    }
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Keep legacy file in sync for backward compatibility.
    (p / CARSALES_URLS_FILENAME).write_text(
        json.dumps({"updated_at": now_iso, "count": len(new_urls), "urls": new_urls}, indent=2),
        encoding="utf-8",
    )
    return target


def load_manual_matches(out_dir: str) -> list[dict[str, Any]]:
    path = _manual_matches_file(Path(out_dir))
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("matches", []) if isinstance(payload, dict) else []
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def save_manual_match_outputs(out_dir: str, match_rows: list[dict[str, Any]]) -> None:
    p = Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)

    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(match_rows),
        "matches": match_rows,
    }
    _manual_matches_file(p).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    csv_path = p / "my_cars.csv"
    fields = [
        "title",
        "price_text",
        "price",
        "odometer_text",
        "odometer_km",
        "detail_url",
        "network_id",
        "card_id",
        "inventory_year_filter",
        "inventory_make_filter",
        "inventory_model_filter",
        "search_url",
        "verified_at",
        "detail_date",
        "first_image_url",
        "detail_page_title",
        "carbarn_evidence",
        "in_date_range",
        "updated_at",
        "dealer_stock_id",
        "dealer_stock_ids",
        "car_code",
        "photo_count",
        "all_image_urls",
        "image_count",
        "vin",
        "registration_plate",
        "body_type",
        "fuel",
        "transmission",
        "dealer_comments",
        "status_label",
        "status_tooltip",
        "price_badge_label",
        "is_sold",
        "matched_stock_no",
        "matched_chassis",
        "match_score",
        "match_reason",
        "match_status",
        "mismatch_count",
        "mismatch_fields",
        "mismatch_messages",
    ]
    now_iso = datetime.now().isoformat(timespec="seconds")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in match_rows:
            if str(row.get("status", "")).lower() not in {"identified", "sold"}:
                continue
            cs = row.get("carsales", {}) if isinstance(row.get("carsales"), dict) else {}
            cb = row.get("carbarn", {}) if isinstance(row.get("carbarn"), dict) else {}
            out_row = {
                "title": cs.get("title", ""),
                "price_text": cs.get("price_text", ""),
                "price": cs.get("price", ""),
                "odometer_text": cs.get("odometer_text", ""),
                "odometer_km": cs.get("odometer_km", ""),
                "detail_url": cs.get("source_url", ""),
                "network_id": cs.get("network_id", ""),
                "card_id": cs.get("network_id", ""),
                "inventory_year_filter": cb.get("year", ""),
                "inventory_make_filter": cb.get("make", ""),
                "inventory_model_filter": cb.get("model", ""),
                "search_url": "",
                "verified_at": now_iso,
                "detail_date": "",
                "first_image_url": cs.get("first_image_url", ""),
                "detail_page_title": cs.get("detail_page_title", "") or cs.get("title", ""),
                "carbarn_evidence": row.get("match_reason", ""),
                "in_date_range": "",
                "updated_at": now_iso,
                "dealer_stock_id": cs.get("dealer_stock_id", ""),
                "dealer_stock_ids": "|".join([str(x) for x in (cs.get("dealer_stock_ids", []) or []) if str(x).strip()]),
                "car_code": cs.get("car_code", ""),
                "photo_count": cs.get("photo_count", ""),
                "all_image_urls": json.dumps(cs.get("all_image_urls", []) or [], ensure_ascii=False),
                "image_count": cs.get("image_count", ""),
                "vin": cs.get("vin", ""),
                "registration_plate": cs.get("registration_plate", ""),
                "body_type": cs.get("body_type", ""),
                "fuel": cs.get("fuel", ""),
                "transmission": cs.get("transmission", ""),
                "dealer_comments": cs.get("dealer_comments", ""),
                "status_label": cs.get("status_label", ""),
                "status_tooltip": cs.get("status_tooltip", ""),
                "price_badge_label": cs.get("price_badge_label", ""),
                "is_sold": bool(cs.get("is_sold", False)),
                "matched_stock_no": cb.get("stock_no", ""),
                "matched_chassis": cb.get("chassis", ""),
                "match_score": row.get("match_score", ""),
                "match_reason": row.get("match_reason", ""),
                "match_status": row.get("status", ""),
                "mismatch_count": row.get("mismatch_count", 0),
                "mismatch_fields": "|".join([str(x) for x in (row.get("mismatch_fields", []) or []) if str(x).strip()]),
                "mismatch_messages": " || ".join([str(x) for x in (row.get("mismatch_messages", []) or []) if str(x).strip()]),
            }
            w.writerow(out_row)


def run_manual_url_job(payload: dict[str, Any]) -> None:
    out_dir = Path((payload.get("out_dir") or DEFAULT_OUTDIR)).resolve()
    session_pool_dir_raw = str(payload.get("session_pool_dir") or "").strip()
    storage_state_raw = str(payload.get("storage_state") or "").strip()
    session_reuse_enabled = bool(payload.get("session_reuse_enabled", False))
    save_storage_state_on_exit = bool(payload.get("save_storage_state_on_exit", False))
    session_runtime = _configure_antibot_session_runtime(
        out_dir=out_dir,
        session_pool_dir_raw=session_pool_dir_raw,
        storage_state_raw=storage_state_raw,
        session_reuse_enabled=session_reuse_enabled,
        save_storage_state_on_exit=save_storage_state_on_exit,
    )
    if bool(session_runtime.get("mode_changed")):
        _reset_antibot_browser()
    run_scope = str(payload.get("run_scope") or "all").strip().lower()
    include_archived_urls = bool(payload.get("include_archived_urls", False))
    requested_stock_ids = _parse_stock_id_tokens(payload.get("stock_ids_text", ""))
    requested_stock_keys = {s.lower() for s in requested_stock_ids}
    payload_urls = payload.get("manual_urls", [])
    if isinstance(payload_urls, list) and payload_urls:
        urls = _extract_manual_urls("\n".join([str(u).strip() for u in payload_urls if str(u).strip()]))
    else:
        urls = load_manual_urls(str(out_dir), include_archived=include_archived_urls)

    # Force browser-assisted workflow to always open browser pages.
    open_urls_in_browser = True
    browser_verification_only = bool(payload.get("browser_verification_only", True))
    recycle_every = int(payload.get("browser_recycle_every", 10) or 10)
    recycle_every = max(1, recycle_every)
    throttle_min_seconds = float(payload.get("request_delay_min_seconds", 6.0) or 6.0)
    throttle_max_seconds = float(payload.get("request_delay_max_seconds", 12.0) or 12.0)
    if throttle_max_seconds < throttle_min_seconds:
        throttle_max_seconds = throttle_min_seconds

    with STATE.lock:
        STATE.running = True
        STATE.stop_requested = False
        STATE.return_code = None
        STATE.started_at = time.time()
        STATE.finished_at = None
        STATE.logs = []
        STATE.last_cmd = ["manual-url-workflow"]
        STATE.last_outdir = str(out_dir)
        STATE.last_error = ""
        STATE.progress = {"stage": "init", "event": "run_started", "progress_percent": 0.0}
        STATE.event_seq += 1
        STATE.progress_updated_at = time.time()

    try:
        if not urls:
            raise ValueError(f"No saved URLs found in {out_dir / CARSALES_URLS_FILENAME}. Save URLs first.")

        out_dir.mkdir(parents=True, exist_ok=True)
        b_ok, b_msg = backup_manual_matches_file(str(out_dir))
        if b_ok:
            STATE.append_log(f"[backup] manual matches snapshot created: {b_msg}")
        else:
            STATE.append_log(f"[backup] skipped: {b_msg}")
        STATE.append_log(
            f"Loaded {len(urls)} URLs from saved registry "
            f"({'active+archived' if include_archived_urls else 'active only'})."
        )
        STATE.append_log(
            f"Manual URL throttling enabled: {throttle_min_seconds:.1f}s-{throttle_max_seconds:.1f}s between requests; "
            f"browser recycle every {recycle_every} URLs."
        )
        STATE.append_log(
            f"[session] pool: {session_runtime['session_pool_dir']} | active: {session_runtime['storage_state_path']}"
        )
        STATE.append_log(
            f"[session] reuse_enabled={bool(session_runtime.get('session_reuse_enabled', True))} "
            f"save_on_exit={bool(session_runtime.get('save_storage_state_on_exit', False))}"
        )

        force_inventory_refresh = bool(payload.get("refresh_inventory", False)) or bool(requested_stock_keys)
        items, inv_error = fetch_carbarn_inventory(force=force_inventory_refresh)
        if not items:
            raise RuntimeError(f"Carbarn API inventory is empty. {inv_error}".strip())
        inventory_rows: list[dict[str, Any]] = []
        scope_counts: dict[str, int] = {
            "published_scope": 0,
            "on_offer": 0,
            "unpublished": 0,
            "excluded_other": 0,
        }
        for v in items:
            row = carbarn_vehicle_to_row(v)
            scope = classify_vehicle_scope(v, row)
            scope_counts[scope] = int(scope_counts.get(scope, 0)) + 1
            if scope == "published_scope":
                inventory_rows.append(row)
        if not inventory_rows:
            raise RuntimeError("No published vehicles found in Carbarn API inventory.")
        STATE.append_log(
            "Inventory scope applied: "
            f"valid={scope_counts.get('published_scope', 0)}, "
            f"on_offer_skipped={scope_counts.get('on_offer', 0)}, "
            f"unpublished_skipped={scope_counts.get('unpublished', 0)}, "
            f"other_skipped={scope_counts.get('excluded_other', 0)}."
        )
        if requested_stock_keys:
            inventory_rows = [
                r for r in inventory_rows if str(r.get("stock_no", "")).strip().lower() in requested_stock_keys
            ]
            if not inventory_rows:
                raise RuntimeError(
                    "No published Carbarn inventory found for requested stock IDs: "
                    + ", ".join(requested_stock_ids)
                )
            STATE.append_log(
                f"Stock filter active: {len(requested_stock_ids)} requested IDs -> "
                f"{len(inventory_rows)} Carbarn inventory records."
            )

        existing_rows = load_manual_matches(str(out_dir))
        if run_scope == "not_found":
            unresolved_url_keys: set[str] = set()
            identified_url_keys: set[str] = set()
            for row in existing_rows:
                if not isinstance(row, dict):
                    continue
                cs = row.get("carsales", {})
                if not isinstance(cs, dict):
                    continue
                key = _norm_url_key(cs.get("source_url", ""))
                if not key:
                    continue
                status = str(row.get("status", "")).strip().lower()
                if status in {"identified", "sold"}:
                    identified_url_keys.add(key)
                else:
                    unresolved_url_keys.add(key)
            unresolved_url_keys.update(
                {_norm_url_key(u) for u in urls if _norm_url_key(u) and _norm_url_key(u) not in identified_url_keys}
            )
            filtered_urls = [u for u in urls if _norm_url_key(u) in unresolved_url_keys]
            if not filtered_urls:
                if requested_stock_keys:
                    STATE.append_log(
                        "Run scope 'not_found' has no unresolved URLs, but stock IDs were selected. "
                        "Falling back to all saved URLs so stock filter can run."
                    )
                else:
                    raise RuntimeError("No unresolved URLs available to run (all saved URLs are currently identified/sold).")
            else:
                urls = filtered_urls
                STATE.append_log(
                    f"Run scope 'not_found': processing {len(urls)} unresolved URLs (not_found/error/unmatched)."
                )
        if requested_stock_keys:
            url_stock_keys: dict[str, set[str]] = {}
            for row in existing_rows:
                if not isinstance(row, dict):
                    continue
                cs = row.get("carsales", {})
                if not isinstance(cs, dict):
                    continue
                url_key = _norm_url_key(cs.get("source_url", ""))
                if not url_key:
                    continue
                url_stock_keys[url_key] = _extract_row_stock_ids(row)
            filtered_urls: list[str] = []
            for u in urls:
                k = _norm_url_key(u)
                if not k:
                    continue
                stocks = url_stock_keys.get(k, set())
                if stocks & requested_stock_keys:
                    filtered_urls.append(u)
            if not filtered_urls:
                STATE.append_log(
                    "Stock filter fallback: no URL-to-stock mapping found in manual_matches.json. "
                    "Continuing with all saved URLs for this run."
                )
            else:
                urls = filtered_urls
                STATE.append_log(
                    f"URL filter active: processing {len(urls)} URLs mapped to stock IDs: "
                    + ", ".join(requested_stock_ids)
                )

        prior_progress = load_manual_progress(str(out_dir))
        results_by_url: dict[str, dict[str, Any]] = {}
        existing_order_keys: list[str] = []
        for row in existing_rows:
            if not isinstance(row, dict):
                continue
            cs = row.get("carsales", {})
            if not isinstance(cs, dict):
                continue
            key = _norm_url_key(cs.get("source_url", ""))
            if key:
                results_by_url[key] = row
                existing_order_keys.append(key)

        def _rows_for_persist() -> list[dict[str, Any]]:
            seen: set[str] = set()
            out_rows: list[dict[str, Any]] = []
            for key in existing_order_keys:
                row = results_by_url.get(key)
                if not row or key in seen:
                    continue
                out_rows.append(row)
                seen.add(key)
            for key, row in results_by_url.items():
                if not key or key in seen:
                    continue
                out_rows.append(row)
                seen.add(key)
            return out_rows

        total = len(urls)
        expected_keys = {_norm_url_key(u) for u in urls if _norm_url_key(u)}
        found_count = sum(
            1
            for k, r in results_by_url.items()
            if k in expected_keys and str(r.get("status", "")).lower() in {"identified", "sold"}
        )
        not_found_count = sum(
            1
            for k, r in results_by_url.items()
            if k in expected_keys and str(r.get("status", "")).lower() == "not_found"
        )
        error_count = sum(
            1
            for k, r in results_by_url.items()
            if k in expected_keys and str(r.get("status", "")).lower() == "error"
        )

        pending_urls = [u for u in urls if _norm_url_key(u) not in results_by_url]
        done_before_run = total - len(pending_urls)
        restart_if_complete = bool(payload.get("restart_if_complete", True))
        if total > 0 and done_before_run >= total and restart_if_complete:
            STATE.append_log(
                "Previous checkpoint already marked all URLs as done. "
                "Starting a fresh full pass from URL 1."
            )
            # IMPORTANT: only clear rows for URLs in *this run*.
            # Do not wipe unrelated saved rows (eg selected-stock runs),
            # otherwise unrelated cars disappear and become not_found later.
            for key in list(results_by_url.keys()):
                if key in expected_keys:
                    results_by_url.pop(key, None)
            existing_order_keys = [k for k in existing_order_keys if k not in expected_keys]
            found_count = 0
            not_found_count = 0
            error_count = 0
            pending_urls = list(urls)
            done_before_run = 0
        if done_before_run > 0:
            STATE.append_log(
                f"Resume detected: {done_before_run}/{total} URLs already processed. "
                f"Continuing from next URL."
            )
            if prior_progress:
                STATE.append_log(
                    f"Last checkpoint: {prior_progress.get('last_processed_url', '')} "
                    f"({prior_progress.get('urls_done', done_before_run)}/{prior_progress.get('urls_total', total)})"
                )

        STATE.update_progress(
            {
                "stage": "match",
                "event": "url_processing",
                "current_target_label": pending_urls[0] if pending_urls else "",
                "targets_done": done_before_run,
                "targets_total": total,
                "remaining_count": len(pending_urls),
                "progress_percent": round((done_before_run / max(total, 1)) * 100.0, 2),
                "my_cars_total": found_count,
                "suspected_total": not_found_count,
                "verified_done": done_before_run,
                "cards_collected": done_before_run,
                "skipped_targets_total": error_count,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        )

        for pending_idx, url in enumerate(pending_urls, 1):
            idx = done_before_run + pending_idx
            with STATE.lock:
                if STATE.stop_requested:
                    raise RuntimeError("Run stopped by user.")
            opened_in_antibot = False
            if open_urls_in_browser:
                opened, open_err = _open_url_in_antibot_browser(url)
                if opened:
                    STATE.append_log(f"[anti-bot browser] opened: {url}")
                    opened_in_antibot = True
                else:
                    STATE.append_log(f"[anti-bot browser] failed: {url} | {open_err}")
                # Small immediate pause before detail polling starts.
                if not _sleep_interruptible(0.6):
                    raise RuntimeError("Run stopped by user.")
            STATE.update_progress(
                {
                    "stage": "match",
                    "event": "url_processing",
                    "current_target_label": url,
                    "targets_done": idx - 1,
                    "targets_total": total,
                    "remaining_count": total - (idx - 1),
                    "progress_percent": round(((idx - 1) / max(total, 1)) * 100.0, 2),
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            if opened_in_antibot:
                STATE.append_log("[anti-bot browser] waiting for listing details (solve verification in that window if prompted)...")
                cs, anti_wait_err = _wait_for_antibot_listing_details(
                    url,
                    timeout_seconds=120,
                    poll_seconds=2.0,
                    stop_checker=_is_stop_requested,
                )
                if anti_wait_err == "run_stopped":
                    raise RuntimeError("Run stopped by user.")
                if anti_wait_err:
                    if browser_verification_only:
                        cs["error"] = f"browser_verification_failed: {anti_wait_err}"
                    else:
                        fallback = fetch_carsales_listing(url)
                        if fallback and not fallback.get("error"):
                            cs = fallback
                        else:
                            cs["error"] = (cs.get("error", "") + f"; {anti_wait_err}").strip("; ")
            else:
                if browser_verification_only:
                    reason = "antibot_browser_not_opened" if open_urls_in_browser else "open_urls_in_browser_disabled"
                    cs = _parse_carsales_listing_from_html(url, "")
                    cs["error"] = f"browser_verification_required: {reason}"
                else:
                    cs = fetch_carsales_listing(url)
            if cs.get("error"):
                error_count += 1
                results_by_url[_norm_url_key(url)] = (
                    {
                        "status": "error",
                        "match_score": 0.0,
                        "match_reason": cs.get("error", ""),
                        "carsales": cs,
                        "carbarn": {},
                    }
                )
                STATE.append_log(f"[error] {url}: {cs.get('error')}")
                STATE.update_progress(
                    {
                        "stage": "match",
                        "event": "url_processed",
                        "current_target_label": url,
                        "targets_done": idx,
                        "targets_total": total,
                        "remaining_count": total - idx,
                        "progress_percent": round((idx / max(total, 1)) * 100.0, 2),
                        "my_cars_total": found_count,
                        "suspected_total": not_found_count,
                        "verified_done": idx,
                        "cards_collected": idx,
                        "skipped_targets_total": error_count,
                        "current_page": idx,
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    }
                )
                save_manual_progress(
                    str(out_dir),
                    urls_total=total,
                    urls_done=idx,
                    last_processed_url=url,
                    recycle_every=recycle_every,
                    throttle_min_seconds=throttle_min_seconds,
                    throttle_max_seconds=throttle_max_seconds,
                )
                save_manual_match_outputs(str(out_dir), _rows_for_persist())
                if idx % recycle_every == 0 and open_urls_in_browser:
                    _reset_antibot_browser()
                    STATE.append_log(f"[anti-bot browser] recycled after {idx} processed URLs.")
                if idx < total:
                    wait_s = random.uniform(throttle_min_seconds, throttle_max_seconds)
                    STATE.append_log(f"[throttle] sleeping {wait_s:.1f}s before next URL.")
                    if not _sleep_interruptible(wait_s):
                        raise RuntimeError("Run stopped by user.")
                continue

            matched, score, reason = _match_to_carbarn_inventory(cs, inventory_rows)
            if matched:
                status = "sold" if _is_effectively_sold(cs) else "identified"
                carbarn = matched
                found_count += 1
                mismatch_fields, mismatch_messages = _build_mismatch_report(cs, carbarn)
            else:
                status = "sold" if _is_effectively_sold(cs) else "not_found"
                carbarn = {}
                not_found_count += 1
                mismatch_fields, mismatch_messages = [], []
            results_by_url[_norm_url_key(url)] = (
                {
                    "status": status,
                    "match_score": round(float(score), 2),
                    "match_reason": reason,
                    "mismatch_fields": mismatch_fields,
                    "mismatch_messages": mismatch_messages,
                    "mismatch_count": len(mismatch_fields),
                    "carsales": cs,
                    "carbarn": carbarn,
                }
            )
            STATE.append_log(
                f"[{status}] {url} | {cs.get('year', '')} {cs.get('make', '')} {cs.get('model', '')} | score={score:.1f} | {reason}"
            )
            if mismatch_fields:
                STATE.append_log(f"[mismatch] {url} | {', '.join(mismatch_fields)}")
            STATE.update_progress(
                {
                    "stage": "match",
                    "event": "url_processed",
                    "current_target_label": url,
                    "targets_done": idx,
                    "targets_total": total,
                    "remaining_count": total - idx,
                    "progress_percent": round((idx / max(total, 1)) * 100.0, 2),
                    "my_cars_total": found_count,
                    "suspected_total": not_found_count,
                    "verified_done": idx,
                    "cards_collected": idx,
                    "skipped_targets_total": error_count,
                    "current_page": idx,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            save_manual_progress(
                str(out_dir),
                urls_total=total,
                urls_done=idx,
                last_processed_url=url,
                recycle_every=recycle_every,
                throttle_min_seconds=throttle_min_seconds,
                throttle_max_seconds=throttle_max_seconds,
            )
            save_manual_match_outputs(str(out_dir), _rows_for_persist())
            if idx % recycle_every == 0 and open_urls_in_browser:
                _reset_antibot_browser()
                STATE.append_log(f"[anti-bot browser] recycled after {idx} processed URLs.")
            if idx < total:
                wait_s = random.uniform(throttle_min_seconds, throttle_max_seconds)
                STATE.append_log(f"[throttle] sleeping {wait_s:.1f}s before next URL.")
                if not _sleep_interruptible(wait_s):
                    raise RuntimeError("Run stopped by user.")

        ordered_rows = [results_by_url[k] for k in [_norm_url_key(u) for u in urls] if k in results_by_url]
        save_manual_match_outputs(str(out_dir), _rows_for_persist())
        build_and_save_full_comparison_output(str(out_dir), force_inventory_refresh=False)
        save_manual_progress(
            str(out_dir),
            urls_total=total,
            urls_done=len(ordered_rows),
            last_processed_url=(urls[len(ordered_rows) - 1] if ordered_rows else ""),
            recycle_every=recycle_every,
            throttle_min_seconds=throttle_min_seconds,
            throttle_max_seconds=throttle_max_seconds,
        )
        identified_total = sum(1 for r in ordered_rows if str(r.get("status", "")).lower() in {"identified", "sold"})
        STATE.append_log(f"Manual matching completed. Identified {identified_total}/{len(ordered_rows)}.")
        with STATE.lock:
            STATE.return_code = 0
            STATE.last_error = ""
    except Exception as e:
        with STATE.lock:
            STATE.return_code = 1
            STATE.last_error = str(e)
        STATE.append_log(f"[fatal] {e}")
    finally:
        _reset_antibot_browser()
        with STATE.lock:
            code = int(STATE.return_code or 0)
            STATE.running = False
            STATE.stop_requested = False
            STATE.finished_at = time.time()
            STATE.event_seq += 1
            final_progress = dict(STATE.progress or {})
            final_progress.update(
                {
                    "stage": "finalize",
                    "event": "run_completed" if code == 0 else "run_failed",
                    "progress_percent": 100.0 if code == 0 else float(final_progress.get("progress_percent", 0.0)),
                    "remaining_count": 0,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            STATE.progress = final_progress
            STATE.progress_updated_at = time.time()


def refresh_single_manual_url(
    *,
    out_dir: str,
    source_url: str,
    open_urls_in_browser: bool = True,
    browser_verification_only: bool = True,
    force_inventory_refresh: bool = True,
) -> dict[str, Any]:
    url = str(source_url or "").strip()
    if not url:
        return {"ok": False, "error": "Missing source URL."}
    url_key = _norm_url_key(url)
    if not url_key:
        return {"ok": False, "error": "Invalid source URL."}

    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    backup_manual_matches_file(str(out))

    items, inv_error = fetch_carbarn_inventory(force=force_inventory_refresh)
    if not items:
        return {"ok": False, "error": f"Carbarn API inventory is empty. {inv_error}".strip()}

    inventory_rows: list[dict[str, Any]] = []
    for v in items:
        row = carbarn_vehicle_to_row(v)
        if is_published_vehicle(v, row):
            inventory_rows.append(row)
    if not inventory_rows:
        return {"ok": False, "error": "No published vehicles found in Carbarn API inventory."}

    opened_in_antibot = False
    if open_urls_in_browser:
        opened, _ = _open_url_in_antibot_browser(url)
        opened_in_antibot = bool(opened)

    if opened_in_antibot:
        cs, anti_wait_err = _wait_for_antibot_listing_details(
            url,
            timeout_seconds=120,
            poll_seconds=2.0,
            stop_checker=_is_stop_requested,
        )
        if anti_wait_err:
            if browser_verification_only:
                cs["error"] = f"browser_verification_failed: {anti_wait_err}"
            else:
                fallback = fetch_carsales_listing(url)
                if fallback and not fallback.get("error"):
                    cs = fallback
                else:
                    cs["error"] = (cs.get("error", "") + f"; {anti_wait_err}").strip("; ")
    else:
        if browser_verification_only:
            reason = "antibot_browser_not_opened" if open_urls_in_browser else "open_urls_in_browser_disabled"
            cs = _parse_carsales_listing_from_html(url, "")
            cs["error"] = f"browser_verification_required: {reason}"
        else:
            cs = fetch_carsales_listing(url)

    if cs.get("error"):
        updated_row = {
            "status": "error",
            "match_score": 0.0,
            "match_reason": cs.get("error", ""),
            "carsales": cs,
            "carbarn": {},
        }
    else:
        matched, score, reason = _match_to_carbarn_inventory(cs, inventory_rows)
        if matched:
            mismatch_fields, mismatch_messages = _build_mismatch_report(cs, matched)
            updated_row = {
                "status": ("sold" if _is_effectively_sold(cs) else "identified"),
                "match_score": round(float(score), 2),
                "match_reason": reason,
                "mismatch_fields": mismatch_fields,
                "mismatch_messages": mismatch_messages,
                "mismatch_count": len(mismatch_fields),
                "carsales": cs,
                "carbarn": matched,
            }
        else:
            updated_row = {
                "status": ("sold" if _is_effectively_sold(cs) else "not_found"),
                "match_score": round(float(score), 2),
                "match_reason": reason,
                "mismatch_fields": [],
                "mismatch_messages": [],
                "mismatch_count": 0,
                "carsales": cs,
                "carbarn": {},
            }

    existing_rows = load_manual_matches(str(out))
    ordered: list[dict[str, Any]] = []
    replaced = False
    for row in existing_rows:
        if not isinstance(row, dict):
            continue
        cs_row = row.get("carsales", {})
        key = _norm_url_key(cs_row.get("source_url", "") if isinstance(cs_row, dict) else "")
        if key == url_key:
            ordered.append(updated_row)
            replaced = True
        else:
            ordered.append(row)
    if not replaced:
        ordered.append(updated_row)
    save_manual_match_outputs(str(out), ordered)

    return {
        "ok": True,
        "status": str(updated_row.get("status", "")),
        "match_reason": str(updated_row.get("match_reason", "")),
        "source_url": url,
    }
DEFAULT_PYTHONANYWHERE_FILE_URL = (
    "https://www.pythonanywhere.com/user/carbarnau/files/"
    "home/carbarnau/workflow-test/apps/carsale_scraper/my_car_list.json"
)
DEFAULT_REMOTE_JSON_PATH = "/home/carbarnau/workflow-test/apps/carsale_scraper/my_car_list.json"


def _extract_remote_path_from_pythonanywhere_file_url(file_url: str) -> str:
    parsed = urlparse(str(file_url or "").strip())
    raw_path = unquote(parsed.path or "")
    marker = "/files/"
    idx = raw_path.find(marker)
    if idx < 0:
        return ""
    p = raw_path[idx + len(marker) - 1 :]
    if not p.startswith("/"):
        p = "/" + p
    return p


def _build_pythonanywhere_file_endpoint(host: str, username: str, remote_path: str) -> str:
    clean_host = str(host or "").strip() or "www.pythonanywhere.com"
    clean_user = str(username or "").strip()
    clean_path = "/" + str(remote_path or "").strip().lstrip("/")
    return f"https://{clean_host}/api/v0/user/{clean_user}/files/path{clean_path}"


def _http_get_text(endpoint: str, headers: dict[str, str], timeout: int = 45) -> str:
    if requests is not None:
        resp = requests.get(endpoint, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:400]}")
        return resp.text
    req = Request(endpoint, headers=headers, method="GET")
    with urlopen(req, timeout=timeout) as resp:
        code = getattr(resp, "status", None) or resp.getcode()
        body = resp.read() or b""
    if code != 200:
        text = body.decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {code}: {text[:400]}")
    return body.decode("utf-8", errors="ignore")


def _normalize_url_list(payload: Any) -> list[str]:
    raw_urls: list[str] = []
    if isinstance(payload, list):
        raw_urls = [str(x).strip() for x in payload if str(x).strip()]
    elif isinstance(payload, dict):
        for key in ("urls", "active_urls", "carsales_urls"):
            val = payload.get(key)
            if isinstance(val, list):
                raw_urls = [str(x).strip() for x in val if str(x).strip()]
                break
    urls = _extract_manual_urls("\n".join(raw_urls))
    if not urls:
        raise ValueError("Remote JSON was loaded, but no valid Carsales URLs were found.")
    return urls


def fetch_urls_from_pythonanywhere(
    *,
    username: str,
    token: str,
    host: str,
    remote_json_path: str,
) -> list[str]:
    if not username or not token:
        raise RuntimeError(
            "Missing PythonAnywhere credentials. "
            "Set PYTHONANYWHERE_USERNAME and PYTHONANYWHERE_TOKEN in environment/.env."
        )
    endpoint = _build_pythonanywhere_file_endpoint(host, username, remote_json_path)
    headers = {"Authorization": f"Token {token}"}
    text = _http_get_text(endpoint, headers=headers, timeout=45)
    try:
        payload = json.loads(text)
    except Exception as e:
        raise RuntimeError(f"Failed to parse remote JSON from {remote_json_path}: {e}") from e
    return _normalize_url_list(payload)


def _resolve_remote_json_path(args: argparse.Namespace) -> str:
    if str(args.remote_json_path or "").strip():
        p = str(args.remote_json_path).strip()
    elif str(os.getenv("PYTHONANYWHERE_MY_CAR_LIST_PATH", "")).strip():
        p = str(os.getenv("PYTHONANYWHERE_MY_CAR_LIST_PATH", "")).strip()
    else:
        extracted = _extract_remote_path_from_pythonanywhere_file_url(args.pythonanywhere_file_url)
        p = extracted or DEFAULT_REMOTE_JSON_PATH
    if not p.startswith("/"):
        p = "/" + p
    return p


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Headless Find My Cars runner (standalone, no UI module import).")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUTDIR), help="Output directory.")
    parser.add_argument("--run-scope", default="all", choices=["all", "not_found"])
    parser.add_argument("--stock-ids-text", default="", help="Comma/space-separated stock IDs.")
    parser.add_argument("--open-urls-in-browser", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--browser-verification-only", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--browser-recycle-every", type=int, default=10)
    parser.add_argument("--request-delay-min-seconds", type=float, default=6.0)
    parser.add_argument("--request-delay-max-seconds", type=float, default=12.0)
    parser.add_argument("--refresh-inventory", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--restart-if-complete", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--session-reuse-enabled", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--save-storage-state-on-exit", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--session-pool-dir", default="")
    parser.add_argument("--storage-state", default="")
    parser.add_argument("--pythonanywhere-file-url", default=DEFAULT_PYTHONANYWHERE_FILE_URL)
    parser.add_argument("--remote-json-path", default="")
    parser.add_argument(
        "--pythonanywhere-username",
        default=str(os.getenv("PYTHONANYWHERE_USERNAME", PYTHONANYWHERE_USERNAME)),
    )
    parser.add_argument(
        "--pythonanywhere-token",
        default=str(os.getenv("PYTHONANYWHERE_TOKEN", PYTHONANYWHERE_TOKEN)),
    )
    parser.add_argument(
        "--pythonanywhere-host",
        default=str(os.getenv("PYTHONANYWHERE_HOST", PYTHONANYWHERE_HOST or "www.pythonanywhere.com")),
    )
    return parser


def _print_new_logs(last_index: int) -> int:
    with STATE.lock:
        logs = STATE.logs[last_index:]
        next_index = len(STATE.logs)
    for line in logs:
        print(line, flush=True)
    return next_index


def run_headless(payload: dict[str, Any]) -> int:
    worker = threading.Thread(target=run_manual_url_job, args=(payload,), daemon=True)
    worker.start()
    log_idx = 0
    while worker.is_alive():
        log_idx = _print_new_logs(log_idx)
        time.sleep(0.5)
    worker.join()
    log_idx = _print_new_logs(log_idx)
    with STATE.lock:
        code = int(STATE.return_code or 1)
        err = str(STATE.last_error or "").strip()
    if code != 0 and err:
        print(f"[fatal] {err}", flush=True)
    return code


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    remote_json_path = _resolve_remote_json_path(args)
    urls = fetch_urls_from_pythonanywhere(
        username=str(args.pythonanywhere_username or "").strip(),
        token=str(args.pythonanywhere_token or "").strip(),
        host=str(args.pythonanywhere_host or "").strip(),
        remote_json_path=remote_json_path,
    )
    print(f"Loaded {len(urls)} Carsales URLs from PythonAnywhere: {remote_json_path}", flush=True)

    payload: dict[str, Any] = {
        "out_dir": str(Path(args.out_dir).resolve()),
        "run_scope": str(args.run_scope or "all").strip().lower(),
        "stock_ids_text": str(args.stock_ids_text or ""),
        "manual_urls": urls,
        "open_urls_in_browser": bool(args.open_urls_in_browser),
        "browser_verification_only": bool(args.browser_verification_only),
        "browser_recycle_every": int(args.browser_recycle_every or 10),
        "request_delay_min_seconds": float(args.request_delay_min_seconds or 0.0),
        "request_delay_max_seconds": float(args.request_delay_max_seconds or 0.0),
        "refresh_inventory": bool(args.refresh_inventory),
        "restart_if_complete": bool(args.restart_if_complete),
        "session_reuse_enabled": bool(args.session_reuse_enabled),
        "save_storage_state_on_exit": bool(args.save_storage_state_on_exit),
        "session_pool_dir": str(args.session_pool_dir or "").strip(),
        "storage_state": str(args.storage_state or "").strip(),
    }
    return run_headless(payload)


if __name__ == "__main__":
    raise SystemExit(main())
