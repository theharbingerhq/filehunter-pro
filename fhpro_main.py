#!/usr/bin/env python3
"""File Hunter Pro — intelligent bulk download manager.

Streamlit application that reads a list of direct download links, probes
each for reachability, size, and last-modified date, downloads the
reachable files in the background (oldest first), and packages them into
a zip archive with original timestamps preserved. The archive is handed
to the user only after an explicit confirmation step.

Run locally:
    python -m streamlit run fhpro_main.py
"""

from __future__ import annotations

import os
import re
import time
import uuid
import shutil
import zipfile
import tempfile
import threading
import contextlib
import configparser
import urllib.parse
import email.utils
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import streamlit as st

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# ==========================================================================
# Configuration — branding, paths, limits, and UI values are sourced from config/app_config.ini
# ==========================================================================
APP_DIR = Path(__file__).parent
CONFIG_DIR = APP_DIR / "config"
CONFIG_PATH = CONFIG_DIR / "app_config.ini"

if not CONFIG_PATH.exists():
    raise FileNotFoundError(f"Missing configuration file: {CONFIG_PATH}")

_config = configparser.ConfigParser()
_config.read(CONFIG_PATH, encoding="utf-8")


def cfg(section: str, key: str, fallback: str = "") -> str:
    """Read a string value from app_config.ini with a safe fallback."""
    return _config.get(section, key, fallback=fallback)


def cfg_int(section: str, key: str, fallback: int = 0) -> int:
    """Read an integer value from app_config.ini with a safe fallback."""
    return _config.getint(section, key, fallback=fallback)


# ---- [app] identity / branding ----
APP_NAME = cfg("app", "name", "File Hunter Pro")
APP_BUILD = cfg("app", "version", "0.0.0.0")
APP_DEVELOPER = cfg("app", "developer", "")
APP_ORGANIZATION = cfg("app", "organization", "")
APP_LICENSE = cfg("app", "license", "Freeware")
APP_TAGLINE = cfg("app", "tagline", "")
REPO_URL = cfg("app", "repo_url", "")

# ---- [paths] asset/theme locations, relative to this file ----
ASSETS_DIR = APP_DIR / cfg("paths", "assets_dir", "assets")
THEMES_DIR = APP_DIR / cfg("paths", "themes_dir", "themes")
THEME_CSS_PATH = THEMES_DIR / cfg("paths", "theme_css_file", "app_theme.css")

# ---- [network] ----
HEADERS = {"User-Agent": cfg("network", "user_agent", "FileHunterPro")}

# ---- [limits] .txt upload cap; must also respect server.maxUploadSize ----
MAX_TXT_UPLOAD_MB = cfg_int("limits", "max_txt_upload_mb", 100)
MAX_TXT_UPLOAD_BYTES = MAX_TXT_UPLOAD_MB * 1024 * 1024

# ---- [limits] package-size cap and the buffer above it that hides Download ----
MAX_DOWNLOAD_MB = cfg_int("limits", "max_download_mb", 600)
MAX_DOWNLOAD_BYTES = MAX_DOWNLOAD_MB * 1024 * 1024
DOWNLOAD_HIDE_THRESHOLD_MB = cfg_int("limits", "download_hide_threshold_mb", 610)
DOWNLOAD_HIDE_THRESHOLD_BYTES = DOWNLOAD_HIDE_THRESHOLD_MB * 1024 * 1024

# ---- [limits] single pasted-link download cap (paste-box quick download) ----
SINGLE_LINK_MAX_MB = cfg_int("limits", "single_link_max_mb", MAX_DOWNLOAD_MB)
SINGLE_LINK_MAX_BYTES = SINGLE_LINK_MAX_MB * 1024 * 1024

CHUNK_SIZE = cfg_int("limits", "chunk_size_kb", 256) * 1024
HEAD_TIMEOUT = cfg_int("limits", "head_timeout_sec", 10)
GET_TIMEOUT = cfg_int("limits", "get_timeout_sec", 25)
WORKER_JOIN_TIMEOUT_SEC = cfg_int("limits", "worker_join_timeout_sec", 2)

# ---- [limits] queue processing: retried before being marked failed ----
MAX_RETRIES = cfg_int("limits", "max_retries", 3)
RETRY_BACKOFF_SEC = cfg_int("limits", "retry_backoff_sec", 2)

# ---- [limits] concurrent fetch workers; the zip is still written single-threaded and oldest-first ----
MAX_PARALLEL_DOWNLOADS = cfg_int("limits", "max_parallel_downloads", 4)

# ---- [session] ----
SESSION_ID_PREFIX = cfg("session", "session_id_prefix", "FHPRO_")
SESSION_ID_HEX_LEN = cfg_int("session", "session_id_hex_len", 8)
TEMP_SUBDIR = cfg("session", "temp_subdir", "fhp_sessions")
ZIP_FILENAME = cfg("session", "zip_filename", "fhp_downloads.zip")
ZIP_MIME_TYPE = cfg("session", "zip_mime_type", "application/zip")
FILENAME_MAX_LEN = cfg_int("session", "filename_max_len", 150)
FILENAME_INDEX_WIDTH = cfg_int("session", "filename_index_width", 3)

# ---- [ui] ----
FAVICON_FALLBACK_EMOJI = cfg("ui", "favicon_fallback_emoji", "📦")
PROGRESS_LABEL_TRUNCATE = cfg_int("ui", "progress_label_truncate", 60)
LOG_TAIL_LINES = cfg_int("ui", "log_tail_lines", 300)
LOG_STATUS_COL_WIDTH = cfg_int("ui", "log_status_col_width", 12)
LOG_NAME_COL_WIDTH = cfg_int("ui", "log_name_col_width", 45)
LIVE_REFRESH_SEC = cfg_int("ui", "live_refresh_sec", 1)
FOOTER_REFRESH_SEC = cfg_int("ui", "footer_refresh_sec", 1)
PAGE_LAYOUT = cfg("ui", "page_layout", "centered")
PAGE_SIDEBAR_STATE = cfg("ui", "page_sidebar_state", "collapsed")
UPLOAD_FILE_TYPES = [t.strip() for t in cfg("ui", "upload_file_types", "txt").split(",") if t.strip()]

# Zip format constraint: the legacy DOS date field used by ZipInfo predates 1980.
ZIP_MIN_YEAR = 1980

# Footer copyright year, derived at runtime so it never needs a manual edit.
COPYRIGHT_YEAR = datetime.now().year

# Fixed +05:30 offset (IST has no DST, so no zoneinfo/tzdata dependency is needed)
ZIP_DATE_TZ = timezone(timedelta(hours=5, minutes=30), name="IST")

# ==========================================================================
# Icon / logo asset registry
# ==========================================================================
# Only three real image/SVG assets are used: the hero logo, the favicon, and the footer repo mark.
# Every other icon uses Streamlit's built-in `:material/name:` shortcode instead of a custom file.
ICON_ASSETS = {
    "app_logo": {"svg": ASSETS_DIR / cfg("assets", "app_logo_svg", "app_logo.svg")},
    "app_icon": {"ico": ASSETS_DIR / cfg("assets", "app_logo_ico", "app_logo.ico")},
    "source": {"svg": ASSETS_DIR / cfg("assets", "git_logo_svg", "git_logo.svg")},
}


def _parse_crop(raw: str) -> tuple[int, int, int, int] | None:
    """Parse a 'min_x,min_y,width,height' ini value into an int tuple, or None."""
    if not raw:
        return None
    try:
        parts = tuple(int(p.strip()) for p in raw.split(","))
        return parts if len(parts) == 4 else None
    except ValueError:
        return None


# Per-icon viewBox crop overrides for source SVGs with extra canvas margin.
ICON_CROPS = {k: v for k, v in {"app_logo": _parse_crop(cfg("assets", "app_logo_svg_crop", ""))}.items() if v}


def icon_file_path(name: str, kind: str = "ico") -> str | None:
    """Resolve a filesystem path for a non-inline icon (currently the favicon only)."""
    path = ICON_ASSETS.get(name, {}).get(kind)
    return str(path) if path and path.exists() else None


# ==========================================================================
# External font/icon resources — loaded via <link> tags (not @import) so the browser fetches
# them in parallel; Bootstrap Icons falls back from jsDelivr to cdnjs if the primary CDN fails.
# ==========================================================================
st.markdown(
    """<link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link rel="stylesheet"
    href="https://fonts.googleapis.com/css2?family=Manrope:wght@500;600;700;800&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;600&display=swap">
    <link rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css"
    onerror="this.onerror=null;this.href='https://cdnjs.cloudflare.com/ajax/libs/bootstrap-icons/1.11.3/font/bootstrap-icons.min.css';">""",
    unsafe_allow_html=True,
)

# ==========================================================================
# Page config — favicon + title; must run before any other st.* call.
# ==========================================================================
st.set_page_config(
    page_title=APP_NAME,
    page_icon=icon_file_path("app_icon", "ico") or FAVICON_FALLBACK_EMOJI,
    layout=PAGE_LAYOUT,
    initial_sidebar_state=PAGE_SIDEBAR_STATE,
)


# ==========================================================================
# Formatting helpers
# ==========================================================================
def human_size(num_bytes: float | None) -> str:
    """Format a byte count as a human-readable size string."""
    if num_bytes is None:
        return "Unknown"
    n = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_duration(seconds: float) -> str:
    """Format a second count as a compact hours/minutes/seconds string."""
    seconds = max(int(seconds), 0)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def system_stats() -> dict:
    """Return a snapshot of CPU%, RAM%, RAM used, and active thread count for the live dashboard."""
    if not PSUTIL_AVAILABLE:
        return {"cpu": "N/A", "ram_pct": "N/A", "ram_used": "N/A", "threads": threading.active_count()}
    return {
        "cpu": f"{psutil.cpu_percent(interval=None):.0f}%",
        "ram_pct": f"{psutil.virtual_memory().percent:.0f}%",
        "ram_used": human_size(psutil.virtual_memory().used),
        "threads": threading.active_count(),
    }


def speed_and_eta(progress: dict) -> tuple[str | None, str | None]:
    """Compute (speed, eta) for the active download phase, or (None, None) if not enough data yet."""
    start = progress.get("start_time")
    done = progress.get("bytes_done") or 0
    total = progress.get("bytes_total") or 0
    if not start or done <= 0:
        return None, None
    elapsed = time.time() - start
    if elapsed <= 0:
        return None, None
    bps = done / elapsed
    speed = f"{human_size(bps)}/s"
    eta = human_duration((total - done) / bps) if total > done and bps > 0 else "—"
    return speed, eta


def batch_timeline_html(n_batches: int, current_index: int) -> str:
    """Render a ●───●───○───○ batch timeline (solid = done, glowing = active, hollow = pending)."""
    dots = []
    for i in range(n_batches):
        state = "done" if i < current_index else ("active" if i == current_index else "pending")
        dots.append(f'<span class="fhp-timeline-dot {state}" title="Batch {i + 1} of {n_batches}"></span>')
        if i < n_batches - 1:
            line_state = "done" if i < current_index else "pending"
            dots.append(f'<span class="fhp-timeline-line {line_state}"></span>')
    return f'<div class="fhp-timeline">{"".join(dots)}</div>'


def guess_filename(url: str) -> str:
    """Derive a display filename from a URL's path component."""
    path = urllib.parse.urlparse(url).path
    return urllib.parse.unquote(os.path.basename(path) or "file")


def extract_filename(content_disposition: str, url: str) -> str:
    """Prefer the server-provided Content-Disposition filename, else fall back to the URL."""
    if content_disposition:
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition)
        if match:
            return urllib.parse.unquote(match.group(1).strip())
    return guess_filename(url)


def safe_filename(name: str, index: int) -> str:
    """Strip filesystem-unsafe characters from a name; index guarantees a non-empty fallback."""
    name = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    return name or f"file_{index}"


def parse_last_modified(header_value: str) -> datetime | None:
    """Parse an HTTP Last-Modified header into a datetime; callers must treat None as 'unknown', not 'oldest'."""
    if not header_value:
        return None
    try:
        return email.utils.parsedate_to_datetime(header_value)
    except (TypeError, ValueError):
        return None


def zip_date_stamp() -> str:
    """Return today's date in Asia/Chennai (+05:30) as YYYYMMDD, for zip filenames."""
    return datetime.now(ZIP_DATE_TZ).strftime("%Y%m%d")


def dated_zip_filename() -> str:
    """Build the default date-stamped zip filename, e.g. fhp_downloads_20260722.zip."""
    stem, suffix = Path(ZIP_FILENAME).stem, Path(ZIP_FILENAME).suffix
    return f"{stem}_{zip_date_stamp()}{suffix}"


def batch_zip_filename(idx: int) -> str:
    """Build a per-batch zip filename, e.g. fhp_downloads_b01_20260722.zip."""
    stem, suffix = Path(ZIP_FILENAME).stem, Path(ZIP_FILENAME).suffix
    return f"{stem}_b{idx + 1:02d}_{zip_date_stamp()}{suffix}"


def build_batches(reachable: list[dict], cap_bytes: int) -> tuple[list[list[dict]], list[bool]]:
    """Greedily split reachable entries into oldest-first batches that each stay under cap_bytes."""
    ordered = sorted(reachable, key=lambda a: (a["last_modified_dt"] is None, a["last_modified_dt"]))
    batches: list[list[dict]] = []
    oversized_flags: list[bool] = []
    current: list[dict] = []
    current_size = 0
    for entry in ordered:
        size = entry["size_bytes"] or 0
        if current and current_size + size > cap_bytes:
            batches.append(current)
            oversized_flags.append(current_size > cap_bytes)
            current, current_size = [], 0
        current.append(entry)
        current_size += size
    if current:
        batches.append(current)
        oversized_flags.append(current_size > cap_bytes)
    return batches, oversized_flags


def fresh_progress() -> dict:
    """Return a blank progress-tracking dict shared between a worker thread and the UI."""
    return {
        "phase": None,  # "analyze" | "download"
        "total": 0,
        "completed": 0,
        "current": "",
        "current_size": "",
        "current_files": [],
        "running": False,
        "stopped": False,
        "error": None,
        "log": [],
        "zip_path": None,
        "zip_count": 0,
        "zip_size": 0,
        "start_time": None,
        "bytes_total": 0,
        "bytes_done": 0,
        "bytes_discovered": 0,
    }


def get_work_dir() -> Path:
    """Return (creating if needed) this session's isolated temp working directory."""
    wd = Path(tempfile.gettempdir()) / TEMP_SUBDIR / st.session_state.session_id
    wd.mkdir(parents=True, exist_ok=True)
    return wd


@st.cache_resource(show_spinner=False)
def load_inline_svg(
    path: Path, crop_viewbox: tuple[int, int, int, int] | None = None, size_px: int | None = None,
) -> str:
    """Load SVG markup for inlining, optionally cropping its viewBox and baking in a fixed display size
    (``size_px``) so the browser doesn't briefly flash the source file's native intrinsic size on load."""
    if not path.exists():
        return ""
    raw = path.read_text()
    if crop_viewbox:
        min_x, min_y, w, h = crop_viewbox
        new_viewbox = f'viewBox="{min_x} {min_y} {w} {h}"'
        raw, n = re.subn(r'viewBox="[^"]*"', new_viewbox, raw, count=1)
        if n == 0:
            raw = re.sub(r"<svg\b", f"<svg {new_viewbox}", raw, count=1)
    if size_px is not None:
        raw = re.sub(r'\s(width|height)="[^"]*"', "", raw)
        raw = re.sub(r"<svg\b", f'<svg width="{size_px}" height="{size_px}"', raw, count=1)
    # Collapse inter-tag whitespace so Streamlit's markdown parser doesn't mistake it for a code block.
    return re.sub(r">\s+<", "><", raw.strip())


def load_icon(name: str, kind: str = "svg") -> str:
    """Load a registered icon's inline SVG markup, or an empty string if unavailable."""
    path = ICON_ASSETS.get(name, {}).get(kind)
    return load_inline_svg(path, crop_viewbox=ICON_CROPS.get(name)) if path and path.exists() else ""


@st.cache_resource(show_spinner=False)
def load_theme_css(path: Path) -> str:
    """Load the external theme stylesheet, raising rather than rendering unstyled. Cached because this
    reruns on every script rerun, and the file on disk never changes during a running session."""
    if not path.exists():
        raise FileNotFoundError(f"Missing theme stylesheet: {path}")
    return path.read_text(encoding="utf-8")


# ==========================================================================
# Background workers — daemon threads that mutate shared dict/list objects
# in place so the main script can read live progress on every rerun.
# ==========================================================================
def probe_url(url: str) -> dict:
    """Probe a single URL via HEAD (falling back to a streamed GET) and return a reachability/size/mtime entry."""
    entry = {
        "url": url,
        "filename": guess_filename(url),
        "status": "checking",
        "size_bytes": None,
        "size_human": "-",
        "last_modified_dt": None,
        "last_modified_human": "-",
    }
    try:
        resp = requests.head(url, headers=HEADERS, timeout=HEAD_TIMEOUT, allow_redirects=True)
        if resp.status_code >= 400 or "Content-Length" not in resp.headers:
            resp.close()
            resp = requests.get(url, headers=HEADERS, timeout=HEAD_TIMEOUT, stream=True, allow_redirects=True)
        if resp.status_code < 400:
            size = resp.headers.get("Content-Length")
            entry["status"] = "reachable"
            entry["size_bytes"] = int(size) if size else None
            entry["size_human"] = human_size(int(size)) if size else "Unknown"
            entry["filename"] = extract_filename(resp.headers.get("Content-Disposition", ""), url)
            lm_dt = parse_last_modified(resp.headers.get("Last-Modified"))
            if lm_dt is not None:
                entry["last_modified_dt"] = lm_dt
                entry["last_modified_human"] = lm_dt.strftime("%Y-%m-%d %H:%M")
        else:
            entry["status"] = "unreachable"
            entry["size_human"] = f"HTTP {resp.status_code}"
        resp.close()
    except requests.exceptions.RequestException as exc:
        entry["status"] = "unreachable"
        entry["size_human"] = "Error"
        entry["error"] = str(exc)[:120]
    return entry


def analyze_worker(urls: list[str], analysis_list: list[dict], progress: dict, stop_event: threading.Event) -> None:
    """Probe each URL sequentially, retrying up to MAX_RETRIES times before moving on. Runs in a
    background thread so the UI stays responsive."""
    progress.update(phase="analyze", total=len(urls), completed=0, running=True, stopped=False, error=None)

    for i, url in enumerate(urls):
        if stop_event.is_set():
            progress["stopped"] = True
            break
        progress["current"] = url

        entry = probe_url(url)
        attempt = 1
        while entry["status"] != "reachable" and attempt < MAX_RETRIES and not stop_event.is_set():
            attempt += 1
            progress["log"].append(
                f"{'RETRYING':<{LOG_STATUS_COL_WIDTH}} {entry['filename']:<{LOG_NAME_COL_WIDTH}} attempt {attempt}/{MAX_RETRIES}"
            )
            time.sleep(RETRY_BACKOFF_SEC)
            entry = probe_url(url)

        if stop_event.is_set():
            progress["stopped"] = True
            break

        if entry["size_bytes"]:
            progress["bytes_discovered"] += entry["size_bytes"]
        analysis_list.append(entry)
        progress["completed"] = i + 1
        progress["log"].append(
            f"{entry['status'].upper():<{LOG_STATUS_COL_WIDTH}} "
            f"{entry['filename']:<{LOG_NAME_COL_WIDTH}} {entry['size_human']}"
        )

    progress["running"] = False
    progress["current"] = ""


def _fetch_with_retries(
    entry: dict,
    tmp_path: Path,
    progress: dict,
    stop_event: threading.Event,
    size_cap_bytes: int | None,
    lock: threading.Lock | None = None,
) -> tuple[bool, bool, str | None]:
    """Download one file to tmp_path, retrying up to MAX_RETRIES times on failure.

    Returns (success, aborted, last_error). A stop request or size-cap violation is never
    retried; any other failure (dropped connection, timeout, etc.) is. ``lock`` guards the
    shared ``progress["bytes_done"]`` counter when several downloads run concurrently.
    """
    lock_ctx = lock if lock is not None else contextlib.nullcontext()
    last_error: str | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            progress["log"].append(
                f"{'RETRYING':<{LOG_STATUS_COL_WIDTH}} {entry['filename']:<{LOG_NAME_COL_WIDTH}} attempt {attempt}/{MAX_RETRIES}"
            )
            time.sleep(RETRY_BACKOFF_SEC)

        bytes_written = 0
        try:
            with requests.get(entry["url"], headers=HEADERS, timeout=GET_TIMEOUT, stream=True) as r:
                r.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if stop_event.is_set():
                            with lock_ctx:
                                progress["bytes_done"] -= bytes_written
                            return False, True, None
                        if size_cap_bytes is not None and bytes_written + len(chunk) > size_cap_bytes:
                            with lock_ctx:
                                progress["bytes_done"] -= bytes_written
                            return False, False, f"exceeds {human_size(size_cap_bytes)} limit"
                        if chunk:
                            f.write(chunk)
                            bytes_written += len(chunk)
                            with lock_ctx:
                                progress["bytes_done"] += len(chunk)
            if bytes_written > 0:
                return True, False, None
            last_error = "empty response"
            with lock_ctx:
                progress["bytes_done"] -= bytes_written
        except Exception as exc:
            with lock_ctx:
                progress["bytes_done"] -= bytes_written
            last_error = str(exc)[:80]

    return False, False, last_error


def download_worker(
    analysis_list: list[dict],
    work_dir: Path,
    progress: dict,
    stop_event: threading.Event,
    zip_filename: str | None = None,
    size_cap_bytes: int | None = None,
    max_workers: int | None = None,
) -> None:
    """Fetch reachable files concurrently, then write them to the zip strictly in oldest-first
    order, regardless of which download finished first.

    Two-phase by design: Phase 1 fetches each file in parallel to its own scratch temp file.
    Phase 2 writes the zip single-threaded (``zipfile.ZipFile`` isn't concurrency-safe), walking
    ``reachable`` in its original oldest-first order and preserving each entry's original
    Last-Modified timestamp. Runs in a background thread so the UI stays responsive.
    """
    reachable = [a for a in analysis_list if a["status"] == "reachable"]
    reachable.sort(key=lambda a: (a["last_modified_dt"] is None, a["last_modified_dt"]))

    bytes_total = sum(a["size_bytes"] or 0 for a in reachable)
    progress.update(
        phase="download", total=len(reachable), completed=0, running=True, stopped=False,
        error=None, start_time=time.time(), bytes_total=bytes_total, bytes_done=0,
        current="", current_size="", current_files=[],
    )

    zip_path = work_dir / (zip_filename or dated_zip_filename())
    zip_count = 0
    workers = max(1, min(max_workers or MAX_PARALLEL_DOWNLOADS, len(reachable) or 1))
    progress_lock = threading.Lock()

    # ---- Phase 1: fetch all reachable files concurrently ----
    # results[i] = (tmp_path, success, aborted, last_error); a missing key means it never started.
    results: dict[int, tuple[Path, bool, bool, str | None]] = {}

    def fetch_one(i: int, entry: dict) -> tuple[int, Path | None, bool, bool, str | None]:
        if stop_event.is_set():
            return i, None, False, True, None

        tmp_path = work_dir / f"__fetch_{uuid.uuid4().hex}.part"
        with progress_lock:
            progress["current_files"].append(entry["filename"])
            progress["current"] = ", ".join(progress["current_files"])
            progress["current_size"] = f"{len(progress['current_files'])} in progress"

        success, aborted, last_error = _fetch_with_retries(
            entry, tmp_path, progress, stop_event, size_cap_bytes, lock=progress_lock,
        )

        with progress_lock:
            if entry["filename"] in progress["current_files"]:
                progress["current_files"].remove(entry["filename"])
            progress["current"] = ", ".join(progress["current_files"])
            progress["current_size"] = f"{len(progress['current_files'])} in progress" if progress["current_files"] else ""
            progress["completed"] += 1

        return i, tmp_path, success, aborted, last_error

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_one, i, entry) for i, entry in enumerate(reachable)]
        for fut in as_completed(futures):
            i, tmp_path, success, aborted, last_error = fut.result()
            results[i] = (tmp_path, success, aborted, last_error)

    if stop_event.is_set():
        progress["stopped"] = True

    # ---- Phase 2: write to the zip, single-threaded, strictly oldest-first ----
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, entry in enumerate(reachable):
            result = results.get(i)
            if result is None:
                continue  # never started — stop was requested before this file's turn
            tmp_path, success, aborted, last_error = result

            if aborted or tmp_path is None:
                progress["log"].append(f"{'CANCELLED':<{LOG_STATUS_COL_WIDTH}} {entry['filename']:<{LOG_NAME_COL_WIDTH}} {entry['size_human']}")
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
                continue

            if success:
                arcname = safe_filename(entry["filename"], i)
                lm_dt = entry.get("last_modified_dt")
                if lm_dt is not None:
                    local_dt = lm_dt.astimezone() if lm_dt.tzinfo else lm_dt
                    if local_dt.year < ZIP_MIN_YEAR:
                        local_dt = datetime.now()
                else:
                    local_dt = datetime.now()

                zip_info = zipfile.ZipInfo(arcname, date_time=local_dt.timetuple()[:6])
                zip_info.compress_type = zipfile.ZIP_DEFLATED
                with open(tmp_path, "rb") as src, zf.open(zip_info, "w") as zdest:
                    shutil.copyfileobj(src, zdest, length=CHUNK_SIZE)
                zip_count += 1
                progress["log"].append(f"{'DOWNLOADED':<{LOG_STATUS_COL_WIDTH}} {entry['filename']:<{LOG_NAME_COL_WIDTH}} {entry['size_human']}")
            else:
                progress["log"].append(
                    f"{'FAILED':<{LOG_STATUS_COL_WIDTH}} {entry['filename']:<{LOG_NAME_COL_WIDTH}} {entry['size_human']} ({last_error})"
                )

            tmp_path.unlink(missing_ok=True)

    progress["zip_path"] = str(zip_path)
    progress["zip_count"] = zip_count
    progress["zip_size"] = zip_path.stat().st_size if zip_path.exists() else 0
    progress["running"] = False
    progress["current"] = ""
    progress["current_files"] = []


# ==========================================================================
# Session state
# ==========================================================================
def init_state() -> None:
    """Populate st.session_state with defaults on first run; a no-op on subsequent reruns."""
    if "session_id" not in st.session_state:
        st.session_state.session_id = SESSION_ID_PREFIX + uuid.uuid4().hex[:SESSION_ID_HEX_LEN].upper()
    defaults = {
        "stage": "idle",  # idle -> analyzing -> analyzed -> downloading -> completed -> all_completed
        "links": [],
        "analysis": [],
        "progress": fresh_progress(),
        "stop_event": threading.Event(),
        "worker_thread": None,
        "show_confirm_dialog": False,
        "download_blocked_msg": None,
        "single_link_error": None,
        "confirm_stop": False,
        "confirm_reset": False,
        "show_settings": False,
        "batches": [],
        "batch_oversized": [],
        "batch_index": 0,
        "batch_downloaded": [],
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def reset_all() -> None:
    """Stop any running worker, wipe this session's temp directory, and clear all session state."""
    stop_event = st.session_state.get("stop_event")
    if stop_event:
        stop_event.set()

    worker = st.session_state.get("worker_thread")
    if worker and worker.is_alive():
        worker.join(timeout=WORKER_JOIN_TIMEOUT_SEC)

    work_dir = Path(tempfile.gettempdir()) / TEMP_SUBDIR / st.session_state.get("session_id", "")
    shutil.rmtree(work_dir, ignore_errors=True)
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.rerun()


# ==========================================================================
# Modal dialogs
# ==========================================================================
@st.dialog(":material/settings: Settings")
def settings_dialog() -> None:
    """Session details and a confirm-gated full application reset."""
    st.caption("Session details, current limits, and app reset.")
    st.markdown(
        """<hr class="fhp-divider">""",
        unsafe_allow_html=True,
    )
    if st.session_state.confirm_reset:
        st.warning(
            "This clears your uploaded links, scan results, and any downloaded "
            "files for the current session. This action cannot be undone. Do you want to continue?"
        )
        cancel_col, reset_col = st.columns(2, gap="small")
        with cancel_col:
            if st.button("Cancel", icon=":material/close:", width="stretch", key="cancel_reset"):
                st.session_state.confirm_reset = False
                st.session_state.show_settings = True
                st.rerun()
        with reset_col:
            if st.button(
                "Yes, reset", icon=":material/restart_alt:", type="primary",
                width="stretch", key="confirm_reset_btn",
            ):
                st.session_state.show_settings = False
                st.session_state.confirm_reset = False
                reset_all()
    else:
        st.button(
            "Reset Application", icon=":material/restart_alt:", type="primary", width="stretch",
            on_click=lambda: (
                st.session_state.__setitem__("confirm_reset", True),
                st.session_state.__setitem__("show_settings", True),
            ),
        )
        if st.session_state.confirm_reset:
            st.rerun()


@st.dialog(":material/info: About Us")
def about_dialog() -> None:
    """App identity, license, platform, and build metadata."""
    st.markdown(
        f"""**{APP_NAME}** is a modern batch download manager designed for speed and simplicity.
        It verifies your download links, retrieves valid files, and packages them into a single ZIP
        archive while preserving their original file timestamps.""",
        unsafe_allow_html=True,
    )
    col1, col2, col3 = st.columns(3)
    col1.markdown(
        f'<i class="bi bi-patch-check-fill fhp-icon fhp-icon-success"></i><b>{APP_LICENSE}</b>',
        unsafe_allow_html=True,
    )
    col2.markdown(
        '<i class="bi bi-globe fhp-icon fhp-icon-info"></i><b>WebApp</b>',
        unsafe_allow_html=True,
    )
    col3.markdown(
        f'<i class="bi bi-box-seam-fill fhp-icon fhp-icon-warning"></i><b>{APP_BUILD}</b>',
        unsafe_allow_html=True,
    )
    st.markdown(
        """<hr class="fhp-divider">""",
        unsafe_allow_html=True,
    )
    st.caption(f"Made with ❤️ by **{APP_DEVELOPER}**")
    st.caption(f"**{APP_ORGANIZATION}**")


@st.dialog(":material/help: How to Use?")
def help_dialog() -> None:
    """Step-by-step usage guide covering upload, analyze, download, and save."""
    st.markdown(
        """<h5><i class="bi bi-info-circle-fill fhp-icon fhp-icon-note"></i>Important Notes</h5>""",
        unsafe_allow_html=True,
    )
    col1, col2 = st.columns(2)
    col1.caption(f"Upload Limit: **{MAX_TXT_UPLOAD_MB} MB**")
    col2.caption(f"Download Zip Limit: **{MAX_DOWNLOAD_MB} MB**")
    st.markdown(
        """<hr class="fhp-divider">""",
        unsafe_allow_html=True,
    )
    st.markdown("### File Hunter Pro — Quick Guide")
    st.caption("Follow these simple steps to create your download package.")
    st.markdown(
        f"""<b><i class="bi bi-upload fhp-icon fhp-icon-success"></i> Step 1 — Upload Links</b><br>
        Upload a <b>.txt</b> file (up to <b>{MAX_TXT_UPLOAD_MB} MB</b>) containing <b>one direct download link per line</b>.
        Blank lines and duplicate links are ignored.""",
        unsafe_allow_html=True,
    )
    st.markdown(
        """<b><i class="bi bi-search fhp-icon fhp-icon-info"></i> Step 2 — Analyze Links</b><br>
        Each link is verified for accessibility, file size, and last modified date. Invalid or unreachable
        links are identified before downloading.""",
        unsafe_allow_html=True,
    )
    st.markdown(
        """<b><i class="bi bi-download fhp-icon fhp-icon-warning"></i> Step 3 — Download & Package</b><br>
        Only valid files are downloaded in <b>oldest-first</b> order and automatically packaged into a single
        ZIP archive while preserving their original timestamps.""",
        unsafe_allow_html=True,
    )
    st.markdown(
        """<b><i class="bi bi-check2-circle fhp-icon fhp-icon-complete"></i> Step 4 — Save Package</b><br>
        Review the package summary and download the generated ZIP archive to your device.""",
        unsafe_allow_html=True,
    )


@st.dialog("Confirm Download")
def confirm_download_dialog() -> None:
    """Final save confirmation; in batch mode this also advances to the next batch."""
    p = st.session_state.progress
    zip_path = p.get("zip_path")
    zip_count = p.get("zip_count", 0)
    zip_size = p.get("zip_size", 0)
    download_name = os.path.basename(zip_path) if zip_path else dated_zip_filename()

    st.write(f"You're about to save **{download_name}** to your device.")
    dc1, dc2 = st.columns(2)
    dc1.metric("Files", zip_count)
    dc2.metric("Package Size", human_size(zip_size))

    # No Cancel option by design: once a batch has finished, saving it is the only path forward.
    in_batch_mode = bool(st.session_state.batches)
    if in_batch_mode:
        idx = st.session_state.batch_index
        has_next_batch = (idx + 1) < len(st.session_state.batches)
        confirm_label = f"Save & Start Batch {idx + 2:03d}" if has_next_batch else "Save & Finish"
    else:
        confirm_label = "Confirm & Save"

    if zip_path and os.path.exists(zip_path):
        with open(zip_path, "rb") as f:
            st.download_button(
                confirm_label, data=f.read(), file_name=download_name, mime=ZIP_MIME_TYPE,
                type="primary", width="stretch", on_click=_finalize_confirmed_download,
            )


def _finalize_confirmed_download() -> None:
    """Close the confirm dialog and, in batch mode, unlock the next batch or mark all batches complete."""
    st.session_state.show_confirm_dialog = False
    if st.session_state.batches:
        idx = st.session_state.batch_index
        st.session_state.batch_downloaded[idx] = True

        zip_path = st.session_state.progress.get("zip_path")
        if zip_path and os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except OSError:
                pass

        next_idx = idx + 1
        st.session_state.batch_index = next_idx
        st.session_state.stage = "all_completed" if next_idx >= len(st.session_state.batches) else "analyzed"


# ==========================================================================
# App bootstrap
# ==========================================================================
init_state()

# Styling loaded from the external theme stylesheet; edit themes/app_theme.css, not this file.
st.markdown(f"<style>\n{load_theme_css(THEME_CSS_PATH)}\n</style>", unsafe_allow_html=True)

logo_svg_markup = load_icon("app_logo")
git_logo_svg_markup = load_icon("source")

# ==========================================================================
# Hero + navigation — branding/session header and nav buttons share one bordered
# glass container (see .st-key-fhp_hero in app_theme.css) instead of stacking as two cards.
# ==========================================================================
with st.container(border=True, key="fhp_hero"):
    st.markdown(
        f"""<div class="fhp-titlebar">
            <div class="fhp-titlebar-left">
                <span class="fhp-dot red"></span> <span class="fhp-dot yellow"></span> <span class="fhp-dot green"></span>
                <span class="fhp-titlebar-label">Session ID · {st.session_state.session_id}</span>
            </div>
            <div class="fhp-titlebar-right">
                <span class="fhp-chip chip-free">{APP_LICENSE}</span>
                <span class="fhp-chip chip-build">Build {APP_BUILD}</span>
            </div>
        </div>
        <div class="fhp-glitter">
            <span class="g" style="--gy:10%; --gx:8%; --gd:0s;"></span>
            <span class="e" style="--gy:22%; --gx:92%; --gd:0.4s;"></span>
            <span class="t" style="--gy:70%; --gx:15%; --gd:0.9s;"></span>
            <span class="g" style="--gy:80%; --gx:88%; --gd:1.3s;"></span>
            <span class="e" style="--gy:45%; --gx:50%; --gd:1.8s;"></span>
            <span class="g" style="--gy:15%; --gx:60%; --gd:2.2s;"></span>
            <span class="t" style="--gy:60%; --gx:78%; --gd:2.6s;"></span>
            <span class="e" style="--gy:35%; --gx:5%; --gd:1.1s;"></span>
            <span class="g" style="--gy:90%; --gx:35%; --gd:0.7s;"></span>
        </div>
        <div class="fhp-hero-top">
            <div class="fhp-logo-badge">{logo_svg_markup}</div>
            <div> <p class="fhp-title">{APP_NAME}</p> <p class="fhp-subtitle">{APP_TAGLINE}</p> </div>
        </div>""",
        unsafe_allow_html=True,
    )

    st.markdown('<div class="fhp-hero-nav-divider"></div>', unsafe_allow_html=True)

    home_btn, settings_btn, help_btn, about_btn = st.columns(4)
    with home_btn:
        if st.button(
            "Home", icon=":material/home:", width="stretch",
            help="Start a fresh session",
        ):
            reset_all()
    with settings_btn:
        if st.button(
            "Settings", icon=":material/settings:", width="stretch",
            help="Session details & reset",
        ):
            settings_dialog()
    with help_btn:
        if st.button(
            "Help", icon=":material/help:", width="stretch",
            help="How to use File Hunter Pro",
        ):
            help_dialog()
    with about_btn:
        if st.button(
            "About", icon=":material/info:", width="stretch",
            help="App info, license & build",
        ):
            about_dialog()

# ==========================================================================
# Stage stepper — native st.columns + Material icons, highlighting the
# session's current stage; stacks automatically on mobile like the nav bar.
# ==========================================================================
STAGE_STEPS = [
    ("upload_file", "Upload", ("idle",)),
    ("search", "Analyze", ("analyzing", "analyzed")),
    ("download", "Download", ("downloading",)),
    ("task_alt", "Complete", ("completed", "all_completed")),
]
current_stage = st.session_state.get("stage", "idle")
with st.container(key="fhp_stepper"):
    for col, (icon, label, stage_keys) in zip(st.columns(4), STAGE_STEPS):
        with col:
            st.button(
                label, icon=f":material/{icon}:", disabled=True, width="stretch",
                type="primary" if current_stage in stage_keys else "secondary", key=f"stage_{label}",
            )

st.write("")

# ==========================================================================
# Stage: idle — upload the link list
# ==========================================================================
if st.session_state.stage == "idle":
    tab_list, tab_single = st.tabs([":material/upload_file: Upload Link List", ":material/link: Paste Single Link"])

    with tab_list:
        st.markdown(
            f"""<p class="fhp-help-text">
            Upload a <b>.txt</b> file containing one direct download link per line <span class="fhp-help-note">(Max {MAX_TXT_UPLOAD_MB} MB)</span> </p>
            """, unsafe_allow_html=True,
        )
        uploaded = st.file_uploader(
            "Drag & drop your link list, or browse for a file",
            type=UPLOAD_FILE_TYPES, label_visibility="collapsed",
        )

        if uploaded is not None:
            if uploaded.size > MAX_TXT_UPLOAD_BYTES:
                st.error(
                    f"That file is {human_size(uploaded.size)}, which is over the "
                    f"{MAX_TXT_UPLOAD_MB}MB upload limit. Please split it into smaller files."
                )
            else:
                raw = uploaded.read().decode("utf-8", errors="ignore")
                links = [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("#")]
                st.session_state.links = links

                if links:
                    st.info(f"Parsed **{len(links)}** link(s) from `{uploaded.name}`.")
                    if st.button("Analyze Links", icon=":material/search:", type="primary", width="stretch"):
                        st.session_state.analysis = []
                        st.session_state.progress = fresh_progress()
                        st.session_state.stop_event.clear()
                        th = threading.Thread(
                            target=analyze_worker,
                            args=(links, st.session_state.analysis, st.session_state.progress, st.session_state.stop_event),
                            daemon=True,
                        )
                        st.session_state.worker_thread = th
                        th.start()
                        st.session_state.stage = "analyzing"
                        st.rerun()
                else:
                    st.warning("No links found in that file.")

    with tab_single:
        st.markdown(
            f"""<p class="fhp-help-text">
            Paste one direct download link for a quick single-file download
            <span class="fhp-help-note">(Max {SINGLE_LINK_MAX_MB} MB)</span> </p>
            """, unsafe_allow_html=True,
        )
        pasted_url = st.text_input(
            "Paste a direct download link",
            key="single_link_input", label_visibility="collapsed",
            placeholder="https://example.com/path/to/file.zip",
        )

        if st.session_state.get("single_link_error"):
            st.error(st.session_state.single_link_error)

        if st.button(
            "Check & Download", icon=":material/bolt:", type="primary",
            width="stretch", key="single_link_go",
        ):
            url = (pasted_url or "").strip()
            st.session_state.single_link_error = None
            if not url or not re.match(r"^https?://", url, re.IGNORECASE):
                st.session_state.single_link_error = "Paste a valid http(s) direct download link first."
                st.rerun()
            else:
                with st.spinner("Checking link…"):
                    entry = probe_url(url)

                if entry["status"] != "reachable":
                    st.session_state.single_link_error = (
                        f"That link isn't reachable ({entry['size_human']}). Double-check the URL and try again."
                    )
                    st.rerun()
                elif entry["size_bytes"] and entry["size_bytes"] > SINGLE_LINK_MAX_BYTES:
                    st.session_state.single_link_error = (
                        f"That file is {entry['size_human']}, which is over the {SINGLE_LINK_MAX_MB}MB "
                        f"single-link limit. Use the Upload Link List tab to batch/split larger downloads."
                    )
                    st.rerun()
                else:
                    st.session_state.analysis = [entry]
                    st.session_state.links = [url]
                    st.session_state.progress = fresh_progress()
                    st.session_state.stop_event.clear()
                    th = threading.Thread(
                        target=download_worker,
                        args=(st.session_state.analysis, get_work_dir(), st.session_state.progress, st.session_state.stop_event),
                        kwargs={"size_cap_bytes": SINGLE_LINK_MAX_BYTES},
                        daemon=True,
                    )
                    st.session_state.worker_thread = th
                    th.start()
                    st.session_state.stage = "downloading"
                    st.rerun()

# ==========================================================================
# Stage: analyzing / downloading — self-refreshing live progress fragment
# ==========================================================================
if st.session_state.stage in ("analyzing", "downloading"):

    @st.fragment(run_every=LIVE_REFRESH_SEC)
    def progress_fragment() -> None:
        """Poll the shared progress dict and render live metrics, a log tail, and the stop control."""
        p = st.session_state.progress
        is_download = p["phase"] == "download"
        slot = st.empty()

        with slot.container():
            with st.status(
                "Downloading files…" if is_download else "Analyzing links…",
                state="running" if p["running"] else "complete", expanded=True,
            ):
                pct = min(p["completed"] / max(p["total"], 1), 1.0)
                st.progress(pct)
                current_name = p["current"][:PROGRESS_LABEL_TRUNCATE] or "—"

                sys_stats = system_stats()

                if is_download:
                    n_batches = len(st.session_state.get("batches") or [])
                    batch_idx = st.session_state.get("batch_index", 0)
                    batch_label = f"{batch_idx + 1} of {max(n_batches, 1)}"
                    speed, eta = speed_and_eta(p)

                    m1, m2, m3 = st.columns(3)
                    m1.metric("Batch", batch_label)
                    m2.metric("Status", "Downloading")
                    m3.metric("Files", f'{p["completed"]} / {p["total"]}')

                    if n_batches > 1:
                        st.markdown(batch_timeline_html(n_batches, batch_idx), unsafe_allow_html=True)

                    s1, s2, s3 = st.columns(3)
                    s1.metric("Size", f'{human_size(p["bytes_done"])} / {human_size(p["bytes_total"])}')
                    s2.metric("Speed", speed or "Calculating…")
                    s3.metric("ETA", eta or "—")

                    current_size = f" ({p['current_size']})" if p.get("current_size") else ""
                    st.caption(f"**File:** {current_name}{current_size}")
                else:
                    m1, m2, m3 = st.columns(3)
                    m1.metric("Status", "Analyzing")
                    m2.metric("Links", f'{p["completed"]} / {p["total"]}')
                    m3.metric("Discovered", human_size(p["bytes_discovered"]))
                    st.caption(f"**Checking:** {current_name}")

                st.markdown('<div class="fhp-spacer-sm"></div>', unsafe_allow_html=True)
                r1, r2, r3 = st.columns(3)
                r1.metric("CPU", sys_stats["cpu"])
                r2.metric("RAM", f'{sys_stats["ram_pct"]} · {sys_stats["ram_used"]}')
                r3.metric("Threads", sys_stats["threads"])
                if not PSUTIL_AVAILABLE:
                    st.caption("Install `psutil` (`pip install psutil`) to see live CPU/RAM usage.")

                with st.expander("Live log", icon=":material/terminal:", expanded=False):
                    st.code("\n".join(p["log"][-LOG_TAIL_LINES:]) or "…", language=None)

            stop_label = f"Stop {'Download' if is_download else 'Analysis'}"

            if st.session_state.get("confirm_stop"):
                st.warning(f"Stop {'downloading' if is_download else 'analyzing'}? Progress so far will be kept.")
                c1, c2 = st.columns(2, gap="small")
                with c1:
                    if st.button("Cancel", width="stretch"):
                        st.session_state.confirm_stop = False
                        st.rerun(scope="fragment")
                with c2:
                    if st.button("Yes, stop", type="primary", width="stretch"):
                        st.session_state.stop_event.set()
                        st.session_state.confirm_stop = False
                        st.rerun(scope="fragment")
            elif st.button(stop_label, icon=":material/stop:", width="stretch"):
                st.session_state.confirm_stop = True
                st.rerun(scope="fragment")

        if not p["running"]:
            st.session_state.confirm_stop = False
            if st.session_state.stage == "analyzing":
                st.session_state.stage = "analyzed"
            elif st.session_state.stage == "downloading":
                st.session_state.stage = "completed"
            st.rerun()

    progress_fragment()

# ==========================================================================
# Stage: analyzed / completed — scan report + download/save actions
# ==========================================================================
if st.session_state.stage in ("analyzed", "completed"):
    analysis = st.session_state.analysis
    reachable = [a for a in analysis if a["status"] == "reachable"]
    unreachable = [a for a in analysis if a["status"] == "unreachable"]
    total_size = sum(a["size_bytes"] or 0 for a in reachable)
    over_cap = total_size > DOWNLOAD_HIDE_THRESHOLD_BYTES

    # Same oldest-first order the download worker uses, so the report previews the real fetch order.
    sorted_for_display = sorted(
        analysis,
        key=lambda a: (a["status"] != "reachable", a.get("last_modified_dt") is None, a.get("last_modified_dt")),
    )

    st.markdown("###### Scan Summary")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Links", len(analysis))
    m2.metric("Reachable", len(reachable))
    m3.metric("Unreachable", len(unreachable))
    m4.metric("Total Size", human_size(total_size))

    with st.expander("Scan Report", icon=":material/description:", expanded=False):
        report_df = pd.DataFrame(
            [
                {
                    "#": i,
                    "File Name": a["filename"],
                    "Last Modified": a.get("last_modified_human", "-"),
                    "Size": a["size_human"],
                    "Status": "✅ Reachable" if a["status"] == "reachable" else "❌ Unreachable",
                    "URL": a["url"],
                }
                for i, a in enumerate(sorted_for_display, start=1)
            ]
        )
        st.dataframe(
            report_df, width="stretch", hide_index=True,
            column_config={
                "#": st.column_config.NumberColumn(width="small"),
                "URL": st.column_config.LinkColumn(width="medium"),
            },
        )

    # ---- Start download ----
    if st.session_state.stage == "analyzed":
        if not reachable:
            st.warning("No reachable files to download.")
        elif over_cap:
            # Package exceeds the size cap: split into sequential, under-cap batches instead of blocking outright.
            if not st.session_state.batches:
                batches, oversized_flags = build_batches(reachable, MAX_DOWNLOAD_BYTES)
                st.session_state.batches = batches
                st.session_state.batch_oversized = oversized_flags
                st.session_state.batch_downloaded = [False] * len(batches)
                st.session_state.batch_index = 0

            batches = st.session_state.batches
            st.info(
                f"Total size ({human_size(total_size)}) exceeds the {MAX_DOWNLOAD_MB}MB package limit, "
                f"so it's been split into **{len(batches)} batches**. Download them one at a time below — "
                f"each batch's file is removed from the server right after you save it, to stay under the size cap."
            )

            active_idx = st.session_state.batch_index
            for idx, batch in enumerate(batches):
                b_size = sum(e["size_bytes"] or 0 for e in batch)
                done = st.session_state.batch_downloaded[idx]
                oversized = st.session_state.batch_oversized[idx]

                with st.container(border=True):
                    st.markdown(f"**Batch {idx + 1:03d}** — {len(batch)} file(s), {human_size(b_size)}")
                    if oversized:
                        st.markdown(
                            """<i class="bi bi-exclamation-triangle-fill fhp-icon fhp-icon-warning"></i>
                            A single file in this batch is larger than the configured size cap, so this batch
                            still exceeds it. It can't be split any further.""",
                            unsafe_allow_html=True,
                        )

                    if done:
                        st.success("✓ Downloaded")
                    elif idx == active_idx:
                        if st.button(
                            f"Start Download — Batch {idx + 1:03d}", icon=":material/download:",
                            type="primary", width="stretch", key=f"start_batch_{idx}",
                        ):
                            st.session_state.progress = fresh_progress()
                            st.session_state.stop_event.clear()
                            th = threading.Thread(
                                target=download_worker,
                                args=(batch, get_work_dir(), st.session_state.progress, st.session_state.stop_event),
                                kwargs={"zip_filename": batch_zip_filename(idx)},
                                daemon=True,
                            )
                            st.session_state.worker_thread = th
                            th.start()
                            st.session_state.stage = "downloading"
                            st.rerun()
                    else:
                        st.markdown(
                            '<i class="bi bi-lock-fill fhp-icon fhp-icon-warning"></i>'
                            "Locked — finish the batch above first",
                            unsafe_allow_html=True,
                        )
        else:
            if st.button(
                f"Start Download ({len(reachable)} file(s))",
                icon=":material/download:", type="primary", width="stretch",
            ):
                if total_size > MAX_DOWNLOAD_BYTES:
                    # Safety net: size crept above the cap between analysis and click.
                    st.session_state.download_blocked_msg = (
                        f"Package size ({human_size(total_size)}) is over the {MAX_DOWNLOAD_MB}MB limit, "
                        f"so this download can't start. Remove a few links and try again."
                    )
                    st.rerun()
                else:
                    st.session_state.progress = fresh_progress()
                    st.session_state.stop_event.clear()
                    th = threading.Thread(
                        target=download_worker,
                        args=(st.session_state.analysis, get_work_dir(), st.session_state.progress, st.session_state.stop_event),
                        daemon=True,
                    )
                    st.session_state.worker_thread = th
                    th.start()
                    st.session_state.stage = "downloading"
                    st.rerun()

        if st.session_state.get("download_blocked_msg"):
            st.error(st.session_state.download_blocked_msg)

    # ---- Completed: package ready (single package or one batch) ----
    if st.session_state.stage == "completed":
        p = st.session_state.progress
        zip_path = p.get("zip_path")
        zip_count = p.get("zip_count", 0)
        zip_size = p.get("zip_size", 0)
        in_batch_mode = bool(st.session_state.batches)

        if p.get("stopped"):
            st.warning(f"Download stopped early. Packaged {zip_count} file(s) that finished before stopping.")

        if zip_path and zip_count > 0:
            if in_batch_mode:
                idx = st.session_state.batch_index
                n_batches = len(st.session_state.batches)
                st.success(f"Batch {idx + 1:03d} of {n_batches:03d} ready — **{zip_count}** file(s), **{human_size(zip_size)}**.")
                save_label = (
                    f"Save Batch {idx + 1:03d} — Unlock Batch {idx + 2:03d}"
                    if (idx + 1) < n_batches else f"Save Final Batch {idx + 1:03d}"
                )
            else:
                st.success(f"Package ready — **{zip_count}** file(s), **{human_size(zip_size)}**.")
                save_label = "Download Package"
            if st.button(save_label, icon=":material/archive:", type="primary", width="stretch"):
                st.session_state.show_confirm_dialog = True
                st.rerun()
        else:
            st.error("No files were downloaded successfully.")

    # ---- All batches completed ----
    if st.session_state.stage == "all_completed":
        st.success(
            f"All **{len(st.session_state.batches)}** batches downloaded — package complete! "
            f"You can close this tab, or reset the app to start a new list.",
            icon=":material/celebration:",
        )

if st.session_state.get("show_confirm_dialog"):
    confirm_download_dialog()

# ==========================================================================
# FAQ — always shown at the bottom regardless of stage.
# ==========================================================================
st.write("")
st.subheader(":material/quiz: Frequently Asked Questions")

FAQ_ITEMS = [
    (
        "What kind of links can I use?",
        "Direct file links only — links that end in a file type like `.mp4`, `.zip`, or "
        "`.pdf`. A link to a webpage *about* a file (like a video's watch page) won't work.",
    ),
    (
        "Can I download just one file instead of uploading a list?",
        f"Yes — use the **Paste Single Link** tab. Paste a direct download link and it starts right away, "
        f"as long as the file is under {SINGLE_LINK_MAX_MB}MB. For anything larger, use the Upload Link List "
        f"tab instead.",
    ),
    (
        "Why does a link say \"Unreachable\"?",
        "The server didn't respond — the file may be gone, the link may be wrong, or the "
        "server is blocking automated requests. It's just skipped; everything else still runs.",
    ),
    (
        f"Why is there a {MAX_DOWNLOAD_MB}MB limit?",
        "It keeps the app fast and reliable. Go over it, and your files are automatically "
        "split into smaller batches instead of being blocked.",
    ),
    (
        "Why was my download split into batches?",
        "Your files added up to more than the size limit, so the app split them into "
        "smaller batches you save one at a time. Saving one unlocks the next.",
    ),
    (
        "What order do files download in?",
        "Oldest files first, newest last, based on each file's date on the server.",
    ),
    (
        "Do downloaded files keep their original date?",
        "Yes, when the server provides one. If not, the file uses the date it was downloaded.",
    ),
    (
        "What if I stop a download partway through?",
        "Whatever finished downloading is kept and zipped — nothing already saved is lost.",
    ),
    (
        "Is my data private?",
        "Yes. Each session has its own private, temporary folder that no one else can access.",
    ),
    (
        "Do I need to keep this tab open?",
        "Yes — closing the tab ends the session. Keep it open until your download finishes.",
    ),
    (
        "My download won't start. What do I do?",
        "Go to **Settings → Reset App**, remove a few links, and try again with a smaller list.",
    ),
]

for question, answer in FAQ_ITEMS:
    with st.expander(question):
        st.markdown(answer)

# ==========================================================================
# Fixed footer — live engine status + credits (self-refreshing)
# ==========================================================================
ENGINE_STATUS = {
    "idle": ("Active", "status-active"),
    "analyzing": ("Analysing", "status-analysing"),
    "analyzed": ("Ready to Download", "status-ready"),
    "downloading": ("Downloading", "status-downloading"),
    "completed": ("Active", "status-active"),
}


@st.fragment(run_every=FOOTER_REFRESH_SEC)
def render_footer() -> None:
    """Render the fixed footer with the current engine status, copyright line, and repo link."""
    label, dot_cls = ENGINE_STATUS.get(st.session_state.stage, ("Active", "status-active"))
    st.markdown(
        f"""<div class="fhp-footer">
      <div class="fhp-footer-inner">
        <div class="fhp-engine"><span class="dot {dot_cls}"></span>Engine: {label}</div>
        <div class="fhp-footer-mid">
          © {COPYRIGHT_YEAR} <span class="brand">{APP_DEVELOPER}</span>. All rights reserved.
        </div>
        <div class="fhp-footer-right">
          <a class="repo-link" href="{REPO_URL}" target="_blank" rel="noopener noreferrer" title="View source repository">
            <span class="repo-mark">{git_logo_svg_markup}</span>
          </a>
        </div>
      </div>
    </div>""",
        unsafe_allow_html=True,
    )


render_footer()
