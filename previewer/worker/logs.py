"""Build/runtime log files (DATA_DIR/logs/<build-id>.log) and error-summary extraction."""
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_settings

# Odoo log line: "2026-10-30 12:00:00,123 7 ERROR db odoo.modules.loading: message"
_LEVEL = re.compile(r"^\S+ \S+ \d+ (ERROR|CRITICAL) ")
_SUMMARY_LINES = 8
_SUMMARY_CHARS = 2000


def log_path(build_id: int) -> Path:
    return get_settings().logs_dir / f"{build_id}.log"


class BuildLog:
    """Append-only, thread-safe writer shared by the pipeline and container streams."""

    _locks: dict[int, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, build_id: int):
        self.build_id = build_id
        self.path = log_path(build_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locks_guard:
            self.lock = self._locks.setdefault(build_id, threading.Lock())

    def write(self, text: str) -> None:
        with self.lock, open(self.path, "a", encoding="utf-8", errors="replace") as f:
            f.write(text)

    def step(self, message: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.write(f"\n=== [{ts}] {message}\n")

    def stream(self, chunks) -> None:
        """Write a docker logs(stream=True) iterator; returns when it ends."""
        for chunk in chunks:
            self.write(chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk)


def error_summary(text: str, fallback: str = "") -> str:
    """Last ERROR/CRITICAL Odoo log lines (each with its traceback tail), else `fallback`."""
    lines = text.splitlines()
    picked: list[str] = []
    for i, line in enumerate(lines):
        if _LEVEL.match(line):
            block = [line]
            # Keep the final line of a following traceback (the exception message).
            j = i + 1
            while j < len(lines) and not re.match(r"^\S+ \S+ \d+ [A-Z]+ ", lines[j]) \
                    and not lines[j].startswith("==="):
                j += 1
            if j - 1 > i and lines[j - 1].strip():
                block.append("    " + lines[j - 1].strip())
            picked.append("\n".join(block))
    parts = picked[-_SUMMARY_LINES:]
    if fallback:
        parts.insert(0, fallback)
    summary = "\n".join(parts).strip()
    return summary[-_SUMMARY_CHARS:] if summary else "Build failed (no error lines in the log)"


def read_log(build_id: int) -> str:
    p = log_path(build_id)
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
