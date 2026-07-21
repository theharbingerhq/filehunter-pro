#!/usr/bin/env python3

"""
File Hunter Pro 1.26.7.0

A Streamlit app that takes a .txt file of links (one per line), checks each for
reachability/size/last-modified, downloads the reachable ones in the
background (oldest source file first), zips them with each entry's original
Last-Modified timestamp preserved, and hands the package back to the user
after a confirmation step.

Run locally:
    python -m streamlit run FileHunter_Pro_1.26.7.0.py
"""

import os
import re
import io
import time
import uuid
import shutil
import zipfile
import tempfile
import threading
import configparser
import urllib.parse
import email.utils
from pathlib import Path
from datetime import datetime

import requests
import streamlit as st

# --------------------------------------------------------------------------
# Config loading — all branding, paths, limits, and UI values live in
# config/app_config.ini. Edit values there, not here.
# --------------------------------------------------------------------------
APP_DIR = Path(__file__).parent
CONFIG_DIR = APP_DIR / "config"
CONFIG_PATH = CONFIG_DIR / "app_config.ini"

if not CONFIG_PATH.exists():
    raise FileNotFoundError(
        f"Missing configuration file: {CONFIG_PATH}. "
        "File Hunter Pro requires config/app_config.ini to start."
    )

_config = configparser.ConfigParser()
_config.read(CONFIG_PATH, encoding="utf-8")


def cfg(section: str, key: str, fallback: str = "") -> str:
    """String config lookup with fallback."""
    return _config.get(section, key, fallback=fallback)


def cfg_int(section: str, key: str, fallback: int = 0) -> int:
    """Integer config lookup with fallback."""
    return _config.getint(section, key, fallback=fallback)


# ---- [app] — identity / branding ----
APP_NAME = cfg("app", "name", "File Hunter Pro")
APP_BUILD = cfg("app", "version", "0.0.0.0")
APP_DEVELOPER = cfg("app", "developer", "")
APP_LICENSE = cfg("app", "license", "Freeware")
APP_TAGLINE = cfg("app", "tagline", "")
REPO_URL = cfg("app", "repo_url", "")

# ---- [paths] — asset/theme locations, relative to this file ----
ASSETS_DIR = APP_DIR / cfg("paths", "assets_dir", "assets")
THEMES_DIR = APP_DIR / cfg("paths", "themes_dir", "themes")
THEME_CSS_PATH = THEMES_DIR / cfg("paths", "theme_css_file", "app_theme.css")

# ---- [network] ----
HEADERS = {"User-Agent": cfg("network", "user_agent", "FileHunterPro")}

# ---- [limits] ----
# .txt upload cap. MAX_TXT_UPLOAD_MB must be <= server.maxUploadSize in
# .streamlit/config.toml, or the browser blocks the upload before it
# reaches this script.
MAX_TXT_UPLOAD_MB = cfg_int("limits", "max_txt_upload_mb", 200)
MAX_TXT_UPLOAD_BYTES = MAX_TXT_UPLOAD_MB * 1024 * 1024

# Package-size cap. MAX_DOWNLOAD_MB is the advertised hard limit;
# DOWNLOAD_HIDE_THRESHOLD_MB is a small buffer above it — once crossed, the
# Download button is hidden rather than letting a run fail at the last step.
MAX_DOWNLOAD_MB = cfg_int("limits", "max_download_mb", 2000)
MAX_DOWNLOAD_BYTES = MAX_DOWNLOAD_MB * 1024 * 1024
DOWNLOAD_HIDE_THRESHOLD_MB = cfg_int("limits", "download_hide_threshold_mb", 2010)
DOWNLOAD_HIDE_THRESHOLD_BYTES = DOWNLOAD_HIDE_THRESHOLD_MB * 1024 * 1024

CHUNK_SIZE = cfg_int("limits", "chunk_size_kb", 256) * 1024
HEAD_TIMEOUT = cfg_int("limits", "head_timeout_sec", 10)
GET_TIMEOUT = cfg_int("limits", "get_timeout_sec", 25)
WORKER_JOIN_TIMEOUT_SEC = cfg_int("limits", "worker_join_timeout_sec", 2)

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

# Zip format constraint, not a tunable: the legacy DOS date field used by
# ZipInfo cannot represent years before 1980.
ZIP_MIN_YEAR = 1980

# Footer copyright year — derived at runtime so it never needs a manual edit.
COPYRIGHT_YEAR = datetime.now().year

# --------------------------------------------------------------------------
# Icon / logo asset registry
# --------------------------------------------------------------------------
# .ico is used only for the browser tab favicon (st.set_page_config accepts
# a raster/ico path or an emoji, nothing else). .svg is used for every icon
# drawn inside the UI, since SVG inherits theme colors via currentColor/fill
# and stays crisp at any size.
#
# Each entry maps a logical icon name to its asset path(s), built from
# filenames declared in config/app_config.ini's [assets] section. Add new
# icons there, not as a hardcoded Path() elsewhere.
ICON_ASSETS = {
    "app_logo": {"svg": ASSETS_DIR / cfg("assets", "app_logo_svg", "app_logo.svg")},
    "app_icon": {"ico": ASSETS_DIR / cfg("assets", "app_logo_ico", "app_logo.ico")},
    "source": {"svg": ASSETS_DIR / cfg("assets", "git_logo_svg", "git_logo.svg")},
    "settings": {"svg": ASSETS_DIR / cfg("assets", "icon_settings_svg", "icon_settings.svg")},
    "help": {"svg": ASSETS_DIR / cfg("assets", "icon_help_svg", "icon_help.svg")},
    "about": {"svg": ASSETS_DIR / cfg("assets", "icon_about_svg", "icon_about.svg")},
    "faq": {"svg": ASSETS_DIR / cfg("assets", "icon_faq_svg", "icon_faq.svg")},
}


def _parse_crop(raw: str):
    """Parse a 'min_x,min_y,width,height' ini value into an int tuple, or
    None if missing/malformed."""
    if not raw:
        return None
    try:
        parts = tuple(int(p.strip()) for p in raw.split(","))
        return parts if len(parts) == 4 else None
    except ValueError:
        return None


# Per-icon viewBox crop overrides (see load_inline_svg). Use when a source
# .svg has extra canvas margin baked in, instead of re-exporting the file.
ICON_CROPS = {
    "app_logo": _parse_crop(cfg("assets", "app_logo_svg_crop", "")),
}
ICON_CROPS = {k: v for k, v in ICON_CROPS.items() if v is not None}

# Fallback glyphs (24x24, currentColor) shown when the matching asset file
# isn't present yet, so no icon slot ever renders blank.
_FALLBACK_GLYPHS = {
    "app_logo": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0l-4-4m4 4l4-4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>',
    "source": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12c0 4.42 2.87 8.17 6.84 9.5.5.09.68-.22.68-.48v-1.7c-2.78.6-3.37-1.34-3.37-1.34-.46-1.16-1.11-1.47-1.11-1.47-.91-.62.07-.6.07-.6 1 .07 1.53 1.03 1.53 1.03.9 1.53 2.36 1.09 2.94.83.09-.65.35-1.09.63-1.34-2.22-.25-4.56-1.11-4.56-4.95 0-1.1.39-1.99 1.03-2.69-.1-.25-.45-1.27.1-2.65 0 0 .84-.27 2.75 1.02a9.4 9.4 0 0 1 5 0c1.91-1.29 2.75-1.02 2.75-1.02.55 1.38.2 2.4.1 2.65.64.7 1.03 1.59 1.03 2.69 0 3.85-2.34 4.7-4.57 4.95.36.31.68.92.68 1.85v2.74c0 .27.18.58.69.48A10 10 0 0 0 22 12c0-5.52-4.48-10-10-10z"/></svg>',
    "settings": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    "help": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 2-3 4"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    "about": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
    "faq": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><path d="M12 8a1.5 1.5 0 1 1 1.5 1.5c-.6 0-1.1.4-1.3.9"/><line x1="12" y1="12.5" x2="12" y2="12.51"/></svg>',
}


def icon_file_path(name: str, kind: str = "ico"):
    """Filesystem path for non-inline icon uses — currently only the
    favicon, which needs a real path (or emoji), not markup."""
    path = ICON_ASSETS.get(name, {}).get(kind)
    return str(path) if path and path.exists() else None


# --------------------------------------------------------------------------
# Page config (favicon + title) — must run before any other st.* call
# --------------------------------------------------------------------------
st.set_page_config(
    page_title=APP_NAME,
    page_icon=icon_file_path("app_icon", "ico") or FAVICON_FALLBACK_EMOJI,
    layout=PAGE_LAYOUT,
    initial_sidebar_state=PAGE_SIDEBAR_STATE,
)

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def human_size(num_bytes):
    if num_bytes is None:
        return "Unknown"
    n = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_duration(seconds):
    seconds = max(int(seconds), 0)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def speed_and_eta(progress):
    """Returns (speed, eta) for the download phase, or (None, None) if
    there isn't enough data yet to estimate."""
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
    if total > done and bps > 0:
        eta = human_duration((total - done) / bps)
    else:
        eta = "—"
    return speed, eta


def guess_filename(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    name = os.path.basename(path) or "file"
    return urllib.parse.unquote(name)


def extract_filename(content_disposition: str, url: str) -> str:
    if content_disposition:
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition)
        if match:
            return urllib.parse.unquote(match.group(1).strip())
    return guess_filename(url)


def safe_filename(name: str, index: int) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    return name or f"file_{index}"


def parse_last_modified(header_value: str):
    """Parse an HTTP Last-Modified header into a datetime, or None if
    missing/unparsable. Callers must treat None as 'unknown', not 'oldest'."""
    if not header_value:
        return None
    try:
        return email.utils.parsedate_to_datetime(header_value)
    except (TypeError, ValueError):
        return None


def fresh_progress():
    return {
        "phase": None,       # "analyze" | "download"
        "total": 0,
        "completed": 0,
        "current": "",
        "current_size": "",
        "running": False,
        "stopped": False,
        "error": None,
        "log": [],
        "zip_path": None,
        "zip_count": 0,
        "zip_size": 0,
        "start_time": None,      # download phase only, for speed/ETA
        "bytes_total": 0,        # sum of known reachable sizes
        "bytes_done": 0,         # bytes written so far this run
        "bytes_discovered": 0,
    }


def get_work_dir() -> Path:
    wd = Path(tempfile.gettempdir()) / TEMP_SUBDIR / st.session_state.session_id
    wd.mkdir(parents=True, exist_ok=True)
    return wd


def load_inline_svg(path: Path, crop_viewbox: tuple | None = None) -> str:
    """Load an SVG file's markup for inlining.

    crop_viewbox: optional (min_x, min_y, width, height) override for the
    viewBox attribute, for source files with extra canvas margin baked in.
    Rewrites the viewBox so artwork fills its container without needing to
    re-export the file.
    """
    if not path.exists():
        return ""
    raw = path.read_text()
    if crop_viewbox:
        min_x, min_y, w, h = crop_viewbox
        new_viewbox = f'viewBox="{min_x} {min_y} {w} {h}"'
        raw, n = re.subn(r'viewBox="[^"]*"', new_viewbox, raw, count=1)
        if n == 0:
            raw = re.sub(r"<svg\b", f"<svg {new_viewbox}", raw, count=1)
    # Collapse whitespace between tags — Streamlit's markdown parser
    # otherwise mistakes indentation for a code block.
    return re.sub(r">\s+<", "><", raw.strip())


def load_icon(name: str, kind: str = "svg") -> str:
    """Inline SVG markup for a named icon. Loads the real asset off disk if
    present, otherwise falls back to the built-in glyph."""
    path = ICON_ASSETS.get(name, {}).get(kind)
    if path and path.exists():
        return load_inline_svg(path, crop_viewbox=ICON_CROPS.get(name))
    return _FALLBACK_GLYPHS.get(name, "")


def load_theme_css(path: Path) -> str:
    """Load the external theme stylesheet. Raises clearly if missing
    rather than silently rendering an unstyled app."""
    if not path.exists():
        raise FileNotFoundError(
            f"Missing theme stylesheet: {path}. File Hunter Pro requires "
            "the theme CSS file referenced in config/app_config.ini to start."
        )
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Background workers (daemon threads; mutate shared dict/list objects in
# place so the main script can read live progress on every rerun without IPC).
# --------------------------------------------------------------------------
def analyze_worker(urls, analysis_list, progress, stop_event):
    progress.update(phase="analyze", total=len(urls), completed=0,
                     running=True, stopped=False, error=None)
    for i, url in enumerate(urls):
        if stop_event.is_set():
            progress["stopped"] = True
            break
        progress["current"] = url
        entry = {
            "url": url,
            "filename": guess_filename(url),
            "status": "checking",
            "size_bytes": None,
            "size_human": "-",
            "last_modified_dt": None,     # datetime — source's actual mtime
            "last_modified_human": "-",   # display string for the report row
        }
        try:
            resp = requests.head(url, headers=HEADERS, timeout=HEAD_TIMEOUT, allow_redirects=True)
            if resp.status_code >= 400 or "Content-Length" not in resp.headers:
                resp.close()
                resp = requests.get(url, headers=HEADERS, timeout=HEAD_TIMEOUT,
                                     stream=True, allow_redirects=True)
            if resp.status_code < 400:
                size = resp.headers.get("Content-Length")
                entry["status"] = "reachable"
                entry["size_bytes"] = int(size) if size else None
                entry["size_human"] = human_size(int(size)) if size else "Unknown"
                entry["filename"] = extract_filename(resp.headers.get("Content-Disposition", ""), url)
                if entry["size_bytes"]:
                    progress["bytes_discovered"] += entry["size_bytes"]

                lm_dt = parse_last_modified(resp.headers.get("Last-Modified"))
                if lm_dt is not None:
                    entry["last_modified_dt"] = lm_dt
                    local_dt = lm_dt.astimezone() if lm_dt.tzinfo else lm_dt
                    entry["last_modified_human"] = local_dt.strftime("%Y-%m-%d %H:%M")
            else:
                entry["status"] = "unreachable"
                entry["size_human"] = f"HTTP {resp.status_code}"
            resp.close()
        except requests.exceptions.RequestException as exc:
            entry["status"] = "unreachable"
            entry["size_human"] = "Error"
            entry["error"] = str(exc)[:120]

        analysis_list.append(entry)
        progress["completed"] = i + 1
        progress["log"].append(
            f"{entry['status'].upper():<{LOG_STATUS_COL_WIDTH}} "
            f"{entry['filename']:<{LOG_NAME_COL_WIDTH}} {entry['size_human']}"
        )

    progress["running"] = False
    progress["current"] = ""


def download_worker(analysis_list, work_dir, progress, stop_event):
    """Streams each reachable file directly into the zip archive (rather
    than to disk first) to keep peak disk usage close to the final zip
    size instead of ~2x that.

    Files download oldest-source-modified first, newest last; entries with
    no Last-Modified header sort last, since their real age is unknown.
    Each zip entry is stamped with the source's actual Last-Modified time
    when available, so extracted files keep their original modified date.

    A file that fails or is cancelled mid-stream may leave a partial member
    in the zip — harmless (most tools skip/report it as corrupt), but no
    further files are appended after a cancel.
    """
    reachable = [a for a in analysis_list if a["status"] == "reachable"]
    reachable.sort(key=lambda a: (a["last_modified_dt"] is None, a["last_modified_dt"]))

    bytes_total = sum(a["size_bytes"] or 0 for a in reachable)
    progress.update(phase="download", total=len(reachable), completed=0,
                     running=True, stopped=False, error=None,
                     start_time=time.time(), bytes_total=bytes_total, bytes_done=0)

    zip_path = work_dir / ZIP_FILENAME
    zip_count = 0

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, entry in enumerate(reachable):
            if stop_event.is_set():
                progress["stopped"] = True
                break
            progress["current"] = entry["filename"]
            progress["current_size"] = entry.get("size_human", "Unknown")
            arcname = safe_filename(entry["filename"], i)

            # Stamp the zip entry with the source's real Last-Modified time.
            lm_dt = entry.get("last_modified_dt")
            if lm_dt is not None:
                local_dt = lm_dt.astimezone() if lm_dt.tzinfo else lm_dt
                if local_dt.year < ZIP_MIN_YEAR:
                    local_dt = datetime.now()
            else:
                local_dt = datetime.now()

            zip_info = zipfile.ZipInfo(arcname, date_time=local_dt.timetuple()[:6])
            zip_info.compress_type = zipfile.ZIP_DEFLATED

            aborted = False
            failed = False
            bytes_written_this_file = 0
            try:
                with requests.get(entry["url"], headers=HEADERS, timeout=GET_TIMEOUT, stream=True) as r:
                    r.raise_for_status()
                    with zf.open(zip_info, "w") as zdest:
                        for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                            if stop_event.is_set():
                                aborted = True
                                break
                            if chunk:
                                zdest.write(chunk)
                                bytes_written_this_file += len(chunk)
                                progress["bytes_done"] += len(chunk)
            except Exception as exc:
                failed = True
                progress["log"].append(f"FAILED       {entry['filename']:<{LOG_NAME_COL_WIDTH}} {entry['size_human']} ({str(exc)[:80]})")

            if aborted:
                progress["log"].append(f"CANCELLED    {entry['filename']:<{LOG_NAME_COL_WIDTH}} {entry['size_human']}")
                progress["stopped"] = True
                break
            elif not failed and bytes_written_this_file > 0:
                zip_count += 1
                progress["log"].append(f"DOWNLOADED   {entry['filename']:<{LOG_NAME_COL_WIDTH}} {entry['size_human']}")
            elif not failed:
                progress["log"].append(f"FAILED       {entry['filename']:<{LOG_NAME_COL_WIDTH}} {entry['size_human']} (empty response)")

            progress["completed"] = i + 1

    progress["zip_path"] = str(zip_path)
    progress["zip_count"] = zip_count
    progress["zip_size"] = zip_path.stat().st_size if zip_path.exists() else 0
    progress["running"] = False
    progress["current"] = ""


# --------------------------------------------------------------------------
# Session state init
# --------------------------------------------------------------------------
def init_state():
    if "session_id" not in st.session_state:
        st.session_state.session_id = (
            SESSION_ID_PREFIX + uuid.uuid4().hex[:SESSION_ID_HEX_LEN].upper()
        )
    defaults = {
        "stage": "idle",          # idle -> analyzing -> analyzed -> downloading -> completed
        "links": [],
        "analysis": [],
        "progress": fresh_progress(),
        "stop_event": threading.Event(),
        "worker_thread": None,
        "show_confirm_dialog": False,
        "download_blocked_msg": None,
        "confirm_stop": False,
        "confirm_reset": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def reset_all():
    st.session_state.stop_event.set()
    t = st.session_state.get("worker_thread")
    if t and t.is_alive():
        t.join(timeout=WORKER_JOIN_TIMEOUT_SEC)
    wd = Path(tempfile.gettempdir()) / TEMP_SUBDIR / st.session_state.get("session_id", "")
    shutil.rmtree(wd, ignore_errors=True)
    for k in list(st.session_state.keys()):
        del st.session_state[k]
    st.rerun()


init_state()

# --------------------------------------------------------------------------
# Styling — loaded from the external theme stylesheet. Edit
# themes/app_theme.css to change appearance; nothing style-related should
# need to change here.
# --------------------------------------------------------------------------
st.markdown(f"<style>\n{load_theme_css(THEME_CSS_PATH)}\n</style>", unsafe_allow_html=True)

# Inline icon markup used in the hero and footer below.
logo_svg_markup = load_icon("app_logo")
git_logo_svg_markup = load_icon("source")

# --------------------------------------------------------------------------
# Hero pad — logo, title, subtitle, chips, self-contained in one markdown
# call. Settings/Help/About render as a plain row underneath (a div here
# can't wrap widgets from later st calls, so it isn't part of the card).
# --------------------------------------------------------------------------
st.markdown(f"""
<div class="fhp-hero">
  <div class="fhp-glitter">
    <span class="g" style="top:10%; left:8%; animation-delay:0s;"></span>
    <span class="e" style="top:22%; left:92%; animation-delay:0.4s;"></span>
    <span class="t" style="top:70%; left:15%; animation-delay:0.9s;"></span>
    <span class="g" style="top:80%; left:88%; animation-delay:1.3s;"></span>
    <span class="e" style="top:45%; left:50%; animation-delay:1.8s;"></span>
    <span class="g" style="top:15%; left:60%; animation-delay:2.2s;"></span>
    <span class="t" style="top:60%; left:78%; animation-delay:2.6s;"></span>
    <span class="e" style="top:35%; left:5%; animation-delay:1.1s;"></span>
    <span class="g" style="top:90%; left:35%; animation-delay:0.7s;"></span>
  </div>
  <div class="fhp-hero-top">
    <div class="fhp-logo-badge">{logo_svg_markup}</div>
    <div>
      <p class="fhp-title">{APP_NAME}</p>
      <p class="fhp-subtitle">{APP_TAGLINE}</p>
      <div class="fhp-chips">
        <span class="fhp-chip chip-free">{APP_LICENSE}</span>
        <span class="fhp-chip chip-build">Build {APP_BUILD}</span>
      </div>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

b1, b2, b3 = st.columns(3)
with b1:
    with st.popover("Settings", use_container_width=True):
        st.markdown(
            f'<div class="fhp-panel-head"><span class="ico">{load_icon("settings")}</span>'
            f'<span class="ttl">Settings</span></div>'
            f'<div class="fhp-panel-sub">Session details, current limits, and app reset.</div>',
            unsafe_allow_html=True,
        )
        st.caption(f"Session ID: `{st.session_state.session_id}`")
        st.caption(f"App build: {APP_BUILD} • License: {APP_LICENSE}")
        st.divider()
        if st.session_state.confirm_reset:
            st.markdown(
                '<div class="fhp-mini-warn">This clears your uploaded links, scan results, '
                'and any downloaded files for this session. This can\'t be undone — continue?</div>',
                unsafe_allow_html=True,
            )
            rc1, rc2 = st.columns(2, gap="small")
            with rc1:
                if st.button("Cancel", use_container_width=True, key="cancel_reset"):
                    st.session_state.confirm_reset = False
                    st.rerun()
            with rc2:
                if st.button("Yes, reset", type="primary", use_container_width=True, key="confirm_reset_btn"):
                    reset_all()
        else:
            if st.button("↺ Reset App", use_container_width=True,
                         help="Clear this session's links, scan results, and downloaded files"):
                st.session_state.confirm_reset = True
                st.rerun()
with b2:
    with st.popover("Help", use_container_width=True):
        st.markdown(
            f'<div class="fhp-panel-head"><span class="ico">{load_icon("help")}</span>'
            f'<span class="ttl">How It Works</span></div>'
            f'<div class="fhp-panel-sub">Four steps from link list to downloaded package.</div>',
            unsafe_allow_html=True,
        )
        st.caption(f"Upload limit: {MAX_TXT_UPLOAD_MB}MB  •  Package limit: {MAX_DOWNLOAD_MB}MB")
        st.markdown(
            f"1. **Upload** a `.txt` file (max {MAX_TXT_UPLOAD_MB}MB) with one direct "
            "download link per line.\n"
            "2. **Analyze** — each link is checked for reachability, size, and last-modified "
            "date before anything downloads.\n"
            "3. **Download** — reachable files are fetched oldest-first and streamed straight "
            "into a single `.zip` package, each keeping its original modified date.\n"
            "4. **Confirm & save** — review the package size, then save it to your device.\n\n"
            f"Packages are capped at **{MAX_DOWNLOAD_MB}MB (~2GB)** total. If your list is "
            "larger than that, split it across a few `.txt` files and run them one at a time."
        )
with b3:
    with st.popover("About", use_container_width=True):
        st.markdown(
            f'<div class="fhp-panel-head"><span class="ico">{load_icon("about")}</span>'
            f'<span class="ttl">About This App</span></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f"**{APP_NAME}** is a batch download manager: give it a list of "
            "direct links and it checks, fetches, and zips them into one package "
            "for you.\n\n"
            f"License: **{APP_LICENSE}**  •  Build: **{APP_BUILD}**\n\n"
            f"Developed by **{APP_DEVELOPER}**"
        )

st.write("")

# --------------------------------------------------------------------------
# Stage: idle — upload
# --------------------------------------------------------------------------
if st.session_state.stage == "idle":
    st.markdown(
        f'<div class="fhp-card">'
        f'Upload a text file containing one direct '
        f'download link per line. <em>(max {MAX_TXT_UPLOAD_MB}MB)</em>'
        f'</div>',
        unsafe_allow_html=True,
    )
    uploaded = st.file_uploader("Drag & drop your link list, or browse for a file",
                                 type=UPLOAD_FILE_TYPES, label_visibility="collapsed")

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
                if st.button("🔍 Analyze Links", type="primary", use_container_width=True):
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

# --------------------------------------------------------------------------
# Stage: analyzing / downloading — live progress fragment
# --------------------------------------------------------------------------
if st.session_state.stage in ("analyzing", "downloading"):

    @st.fragment(run_every=LIVE_REFRESH_SEC)
    def progress_fragment():
        p = st.session_state.progress
        is_download = p["phase"] == "download"
        label = "Downloading files" if is_download else "Analyzing links"

        with st.container(border=True):
            st.markdown('<div class="fhp-scanbar"><div class="sweep"></div></div>', unsafe_allow_html=True)
            total = max(p["total"], 1)
            pct = min(p["completed"] / total, 1.0)
            downloaded = human_size(p["bytes_done"])
            total = human_size(p["bytes_total"])
            current_size = f" ({p['current_size']})" if is_download and p.get("current_size") else ""
            progress_text = f"{label} • {p['completed']}/{p['total']} Files • {downloaded} / {total} • {p['current'][:PROGRESS_LABEL_TRUNCATE]}{current_size}"
            if is_download:
                speed, eta = speed_and_eta(p)
                if speed:
                    progress_text += f"  •  {speed}  •  ETA {eta}"
            else:
                discovered = human_size(p["bytes_discovered"])
                progress_text = f"{label} • {p['completed']}/{p['total']} Files • {discovered} Discovered • {p['current'][:PROGRESS_LABEL_TRUNCATE]}"
            st.progress(pct, text=progress_text)
            with st.expander("Live log", expanded=False):
                st.code("\n".join(p["log"][-LOG_TAIL_LINES:]) or "…", language=None)

        stop_label = f"⏹ Stop {'Download' if is_download else 'Analysis'}"

        if st.session_state.get("confirm_stop"):
            action = "downloading" if is_download else "analyzing"
            st.markdown(
                f'<div class="fhp-mini-warn">Stop {action}? Progress so far will be kept.</div>',
                unsafe_allow_html=True,
            )
            c1, c2 = st.columns(2, gap="small")
            with c1:
                if st.button("Cancel", use_container_width=True):
                    st.session_state.confirm_stop = False
                    st.rerun()
            with c2:
                if st.button("Yes, stop", type="primary", use_container_width=True):
                    st.session_state.stop_event.set()
                    st.session_state.confirm_stop = False
                    st.rerun()
        else:
            if st.button(stop_label, use_container_width=True):
                st.session_state.confirm_stop = True
                st.rerun()

        if not p["running"]:
            st.session_state.confirm_stop = False
            if st.session_state.stage == "analyzing":
                st.session_state.stage = "analyzed"
            elif st.session_state.stage == "downloading":
                st.session_state.stage = "completed"
            st.rerun()

    progress_fragment()

# --------------------------------------------------------------------------
# Stage: analyzed / completed — report + actions
# --------------------------------------------------------------------------
if st.session_state.stage in ("analyzed", "completed"):
    analysis = st.session_state.analysis
    reachable = [a for a in analysis if a["status"] == "reachable"]
    unreachable = [a for a in analysis if a["status"] == "unreachable"]
    total_size = sum(a["size_bytes"] or 0 for a in reachable)
    over_cap = total_size > DOWNLOAD_HIDE_THRESHOLD_BYTES

    # Display rows in the same oldest-first order the download worker uses,
    # so the report previews the real fetch order.
    sorted_for_display = sorted(
        analysis,
        key=lambda a: (
            a["status"] != "reachable",
            a.get("last_modified_dt") is None,
            a.get("last_modified_dt"),
        ),
    )

    rows_html = ""
    for i, a in enumerate(sorted_for_display, start=1):
        badge_cls = "reachable" if a["status"] == "reachable" else "unreachable"
        badge_txt = "Reachable" if a["status"] == "reachable" else "Unreachable"
        dt_txt = a.get("last_modified_human", "-")
        rows_html += f"""
        <div class="fhp-row" title="{a['url']}">
          <span class="idx">{i:02d}</span>
          <span class="fn">{a['filename']}</span>
          <span class="dt">{dt_txt}</span>
          <span class="sz">{a['size_human']}</span>
          <span class="fhp-badge {badge_cls}">{badge_txt}</span>
        </div>"""

    st.markdown(f"""
    <div class="fhp-card">
      <div class="fhp-card-label">Scan Summary</div>
      <div class="fhp-stats">
        <div class="fhp-stat"><div class="n">{len(analysis)}</div><div class="l">Total Links</div></div>
        <div class="fhp-stat ok"><div class="n">{len(reachable)}</div><div class="l">Reachable</div></div>
        <div class="fhp-stat bad"><div class="n">{len(unreachable)}</div><div class="l">Unreachable</div></div>
        <div class="fhp-stat gold"><div class="n">{human_size(total_size)}</div><div class="l">Total Size</div></div>
      </div>
      <div>{rows_html}</div>
    </div>
    """, unsafe_allow_html=True)

    # --- Start Download ---
    if st.session_state.stage == "analyzed":
        if not reachable:
            st.warning("No reachable files to download.")
        elif over_cap:
            st.error(
                f"Total size ({human_size(total_size)}) exceeds the "
                f"{MAX_DOWNLOAD_MB}MB (~2GB) package limit, so the download "
                f"button is hidden. Remove a few links or split your list into "
                f"smaller batches to bring the total under {MAX_DOWNLOAD_MB}MB."
            )
        else:
            if st.button(f"⬇ Start Download ({len(reachable)} file(s))", type="primary", use_container_width=True):
                if total_size > MAX_DOWNLOAD_BYTES:
                    # Safety net: size crept above the cap between analysis
                    # and click (e.g. re-analysis in another tab).
                    st.session_state.download_blocked_msg = (
                        f"Package size ({human_size(total_size)}) is over the "
                        f"{MAX_DOWNLOAD_MB}MB (~2GB) limit, so this download can't start. "
                        f"Remove a few links and try again."
                    )
                    st.rerun()
                else:
                    work_dir = get_work_dir()
                    st.session_state.progress = fresh_progress()
                    st.session_state.stop_event.clear()
                    th = threading.Thread(
                        target=download_worker,
                        args=(st.session_state.analysis, work_dir, st.session_state.progress, st.session_state.stop_event),
                        daemon=True,
                    )
                    st.session_state.worker_thread = th
                    th.start()
                    st.session_state.stage = "downloading"
                    st.rerun()

        if st.session_state.get("download_blocked_msg"):
            st.error(st.session_state.download_blocked_msg)

    # --- Completed: package ready ---
    if st.session_state.stage == "completed":
        p = st.session_state.progress
        zip_path = p.get("zip_path")
        zip_count = p.get("zip_count", 0)
        zip_size = p.get("zip_size", 0)

        if p.get("stopped"):
            st.warning(f"Download stopped early. Packaged {zip_count} file(s) that finished before stopping.")

        if zip_path and zip_count > 0:
            st.success(f"Package ready — **{zip_count}** file(s), **{human_size(zip_size)}**.")
            if st.button("📦 Download Package", type="primary", use_container_width=True):
                st.session_state.show_confirm_dialog = True
                st.rerun()
        else:
            st.error("No files were downloaded successfully.")


# --------------------------------------------------------------------------
# Confirmation dialog (native Streamlit modal)
# --------------------------------------------------------------------------
@st.dialog("Confirm Download")
def confirm_download_dialog():
    p = st.session_state.progress
    zip_path = p.get("zip_path")
    zip_count = p.get("zip_count", 0)
    zip_size = p.get("zip_size", 0)

    st.write(f"You're about to save **{ZIP_FILENAME}** to your device.")
    st.markdown(f"""
    <div class="fhp-stats">
      <div class="fhp-stat ok"><div class="n">{zip_count}</div><div class="l">Files</div></div>
      <div class="fhp-stat gold"><div class="n">{human_size(zip_size)}</div><div class="l">Package Size</div></div>
    </div>
    """, unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Cancel", use_container_width=True):
            st.session_state.show_confirm_dialog = False
            st.rerun()
    with c2:
        if zip_path and os.path.exists(zip_path):
            with open(zip_path, "rb") as f:
                st.download_button(
                    "Confirm & Save",
                    data=f.read(),
                    file_name=ZIP_FILENAME,
                    mime=ZIP_MIME_TYPE,
                    type="primary",
                    use_container_width=True,
                    on_click=lambda: st.session_state.update(show_confirm_dialog=False),
                )


if st.session_state.get("show_confirm_dialog"):
    confirm_download_dialog()

# --------------------------------------------------------------------------
# FAQ — plain-language answers, always shown at the bottom regardless of stage.
# --------------------------------------------------------------------------
st.write("")
st.markdown(
    f'<div class="fhp-panel-head"><span class="ico">{load_icon("faq")}</span>'
    f'<span class="ttl" style="font-size:1.1rem;">Frequently Asked Questions</span></div>',
    unsafe_allow_html=True,
)

FAQ_ITEMS = [
    (
        "What kind of links can I use?",
        "Any direct download link that points straight to a file — for example, a link "
        "that ends in a file extension like `.mp4`, `.zip`, or `.pdf`. Links to a webpage "
        "*about* a file (like a video's watch page) won't work — the app doesn't search "
        "pages for content, it only fetches links you already have.",
    ),
    (
        "Why does it say a link is \"Unreachable\"?",
        "This means the app tried to reach that link and the server didn't respond "
        "successfully — the file may have been removed, the link may be incorrect, or "
        "the server may be blocking automated requests. Unreachable links are simply "
        "skipped; they don't stop the rest of your list from being analyzed or downloaded.",
    ),
    (
        f"Why is there a {MAX_DOWNLOAD_MB}MB (2GB) limit?",
        "This keeps each session's memory and storage use predictable, especially when "
        "the app is hosted on a server with limited disk space. If your list adds up to "
        "more than that, split it into a few smaller `.txt` files and run them one at a time.",
    ),
    (
        "What order do files download in?",
        "Files are downloaded oldest source file first, newest last, based on each file's "
        "Last-Modified date from the server. Files whose server didn't report a "
        "Last-Modified date are downloaded last, since their real age is unknown.",
    ),
    (
        "Will my files keep their original modified date after unzipping?",
        "Yes, when the source server provides a Last-Modified date — the app stamps each "
        "file inside the zip with that date, so it's preserved after you extract. Not every "
        "server sends this header; when it's missing, the file falls back to the time it was "
        "downloaded. Note this only covers the *modified* date — zip files have no field for "
        "a *created* date, so that will always be set by your OS at the moment you extract.",
    ),
    (
        "What happens if I stop a download partway through?",
        "Any files that finished downloading before you stopped are kept and packaged "
        "normally — you'll get a smaller `.zip` with just those files. Nothing already "
        "saved is lost.",
    ),
    (
        "Is my data private?",
        "Each session works in its own temporary, isolated folder that only your browser "
        "session can access. Files aren't shared between users, and session folders are "
        "cleaned up when the app resets or restarts.",
    ),
    (
        "Do I need to keep this tab open while it works?",
        "Yes — analysis and downloading run in the background while the app is open, but "
        "closing the tab ends the session. For very large batches, keep the tab open (or "
        "in a background window) until the package is ready.",
    ),
    (
        "The download button disappeared. What do I do?",
        f"That means your total is over the {MAX_DOWNLOAD_MB}MB package limit. Use "
        "**Settings → Reset App**, remove some links from your list, and re-upload a "
        "smaller batch.",
    ),
]

for question, answer in FAQ_ITEMS:
    with st.expander(question):
        st.markdown(answer)

# --------------------------------------------------------------------------
# Fixed footer — live engine status + credits (self-refreshing)
# --------------------------------------------------------------------------
ENGINE_STATUS = {
    "idle": ("Active", "status-active"),
    "analyzing": ("Analysing", "status-analysing"),
    "analyzed": ("Ready to Download", "status-ready"),
    "downloading": ("Downloading", "status-downloading"),
    "completed": ("Active", "status-active"),
}


@st.fragment(run_every=FOOTER_REFRESH_SEC)
def render_footer():
    label, dot_cls = ENGINE_STATUS.get(st.session_state.stage, ("Active", "status-active"))
    st.markdown(f"""
    <div class="fhp-footer">
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
    </div>
    """, unsafe_allow_html=True)


render_footer()
