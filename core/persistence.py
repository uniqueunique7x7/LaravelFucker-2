"""Persistence helpers for settings, session restore, and stored scan results.

Designed for 10M+ domain scans:
- Results stored as JSONL (one JSON object per line) — O(1) append, crash-safe.
- Exports stream directly from disk; never load the full file into RAM.
- GUI display uses only the last DISPLAY_LIMIT lines (treeview can't show millions).
- Stat counts are computed by streaming, not by materialising a list.
"""

import csv
import json
from collections import deque
from pathlib import Path
from typing import Any, Dict, Generator, List

CONFIG_FOLDER  = Path("config")
RESULTS_FOLDER = Path("RESULTS")
SESSION_FILE   = CONFIG_FOLDER  / "session.json"
SETTINGS_FILE  = CONFIG_FOLDER  / "settings.json"
RESULTS_FILE   = RESULTS_FOLDER / "results.jsonl"  # one JSON object per line

# How many recent results to load into the GUI treeview on startup.
# The treeview cannot meaningfully display millions of rows.
DISPLAY_LIMIT = 5_000

DEFAULT_SETTINGS: Dict[str, Any] = {
    "thread_count": 100,
    "auto_save": True,
    "last_search": "",
    "window_width": 1320,
    "window_height": 860,
}


# ── Internal helpers ──────────────────────────────────────────────────────

def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _iter_jsonl(path: Path) -> Generator[Dict[str, Any], None, None]:
    """Yield parsed JSON objects from a JSONL file one at a time (no RAM spike)."""
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                pass


# ── Startup / directory setup ───────────────────────────────────────────────

def ensure_persistence() -> None:
    CONFIG_FOLDER.mkdir(exist_ok=True)
    RESULTS_FOLDER.mkdir(exist_ok=True)
    if not SETTINGS_FILE.exists():
        _save_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    if not RESULTS_FILE.exists():
        RESULTS_FILE.touch()


# ── Settings ─────────────────────────────────────────────────────────────

def load_settings() -> Dict[str, Any]:
    ensure_persistence()
    return _load_json(SETTINGS_FILE, DEFAULT_SETTINGS)


def save_settings(settings: Dict[str, Any]) -> None:
    _save_json(SETTINGS_FILE, settings)


# ── Session (scan progress counter) ─────────────────────────────────────────

def load_session() -> Dict[str, Any]:
    return _load_json(SESSION_FILE, {})


def save_session(session_data: Dict[str, Any]) -> None:
    _save_json(SESSION_FILE, session_data)


# ── Result storage ───────────────────────────────────────────────────────────

def append_result(result: Dict[str, Any]) -> None:
    """Append one result as a JSON line — O(1), never reads the existing file."""
    RESULTS_FOLDER.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(result, ensure_ascii=False) + "\n")


def append_text(path: Path, text: str) -> None:
    """Append raw text to a file (category .txt files)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


def clear_results() -> None:
    """Wipe all stored results and the session counter."""
    if RESULTS_FILE.exists():
        RESULTS_FILE.write_text("", encoding="utf-8")
    if SESSION_FILE.exists():
        SESSION_FILE.write_text("{}", encoding="utf-8")


# ── Streaming read API (never loads full file into RAM) ──────────────────────

def count_results_by_status() -> Dict[str, int]:
    """Stream JSONL and return {status: count} without loading into RAM."""
    counts: Dict[str, int] = {}
    for obj in _iter_jsonl(RESULTS_FILE):
        s = obj.get("status", "UNKNOWN")
        counts[s] = counts.get(s, 0) + 1
    return counts


def count_results_total() -> int:
    """Count total non-empty lines in JSONL (fast binary read)."""
    if not RESULTS_FILE.exists():
        return 0
    count = 0
    with open(RESULTS_FILE, "rb") as fh:
        for line in fh:
            if line.strip():
                count += 1
    return count


def tail_results(n: int = DISPLAY_LIMIT) -> List[Dict[str, Any]]:
    """Return the last `n` results for GUI display without loading the whole file."""
    if not RESULTS_FILE.exists():
        return []
    buf: deque = deque(maxlen=n)
    with open(RESULTS_FILE, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if line:
                buf.append(line)
    out = []
    for raw in buf:
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            pass
    return out


def restore_results() -> List[Dict[str, Any]]:
    """Return the last DISPLAY_LIMIT results for the GUI treeview."""
    ensure_persistence()
    return tail_results(DISPLAY_LIMIT)


# ── Export (stream from disk — never materialise full list in RAM) ─────────────

def export_json(dest: Path) -> int:
    """Export all results to a streaming JSON array. Returns number of records."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(dest, "w", encoding="utf-8") as out:
        out.write("[\n")
        first = True
        for obj in _iter_jsonl(RESULTS_FILE):
            if not first:
                out.write(",\n")
            out.write("  " + json.dumps(obj, ensure_ascii=False))
            first = False
            count += 1
        out.write("\n]\n")
    return count


def export_csv(dest: Path) -> int:
    """Export all results to CSV by streaming. Returns number of records."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fields = ["url", "category", "categories", "status", "details"]
    count = 0
    with open(dest, "w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for obj in _iter_jsonl(RESULTS_FILE):
            if "categories" in obj and isinstance(obj["categories"], list):
                obj = {**obj, "categories": ", ".join(obj["categories"])}
            writer.writerow({f: obj.get(f, "") for f in fields})
            count += 1
    return count
