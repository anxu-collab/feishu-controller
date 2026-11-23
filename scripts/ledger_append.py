#!/usr/bin/env python3
"""Feishu Controller JSONL ledger append & query helper.

Optional accelerator script (stdlib-only, Python 3.9+):
- Resolves ledger root: $FEISHU_CONTROLLER_HOME -> <workspace>/.feishu-controller -> ~/.feishu-controller
- Validates ONLY fixed outer envelope fields (payload is industry-specific and left flexible)
- Partitions by <app_token>/YYYY-MM.jsonl in the event's timezone (default Asia/Shanghai)
- Cross-platform lockfile (<app_token>/.lock) with stale-lock recovery and single-write atomic append
- Updates <ledger_root>/recent.json for automatic re-attachment
- Supports querying the latest lens_confirmed / briefing_snapshot event across monthly files
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore
    ZoneInfoNotFoundError = Exception  # type: ignore

ALLOWED_EVENT_TYPES = frozenset(
    {"lens_confirmed", "briefing_snapshot", "bitable_writeback"}
)
DEFAULT_TIMEZONE = "Asia/Shanghai"
SCHEMA_VERSION = 1


def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def _resolve_tz(tz_name: str):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            pass
    if tz_name in ("Asia/Shanghai", "Asia/Chongqing", "PRC", "CST"):
        return timezone(timedelta(hours=8))
    if tz_name in ("UTC", "Etc/UTC", "GMT"):
        return timezone.utc
    return timezone(timedelta(hours=8))


def _candidate_roots(
    explicit_root: Optional[str] = None,
    workspace: Optional[str] = None,
) -> List[Path]:
    candidates: List[Path] = []
    if explicit_root:
        candidates.append(Path(explicit_root).expanduser())
    env_home = os.environ.get("FEISHU_CONTROLLER_HOME", "").strip()
    if env_home:
        candidates.append(Path(env_home).expanduser())
    ws = Path(workspace).expanduser() if workspace else Path.cwd()
    candidates.append(ws / ".feishu-controller")
    candidates.append(Path.home() / ".feishu-controller")

    deduped: List[Path] = []
    seen = set()
    for cand in candidates:
        resolved = cand.resolve() if cand.exists() else cand
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            deduped.append(cand)
    return deduped


def resolve_ledger_root(
    explicit_root: Optional[str] = None,
    workspace: Optional[str] = None,
    *,
    require_writable: bool = True,
) -> Optional[Path]:
    """Return the first usable ledger root in priority order, or None."""
    for cand in _candidate_roots(explicit_root, workspace):
        if not require_writable:
            if cand.is_dir():
                return cand
            continue
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / f".write_probe_{os.getpid()}_{secrets.token_hex(3)}"
            with open(probe, "wb") as fh:
                fh.write(b"")
            probe.unlink(missing_ok=True)
            return cand
        except OSError:
            continue
    return None


def _parse_iso_ts(ts_str: str) -> datetime:
    raw = ts_str.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        raise ValueError("ts must include timezone offset (ISO 8601)")
    return dt


def validate_and_normalize_event(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """Validate fixed outer envelope fields only; return (normalized_event, YYYY-MM)."""
    if not isinstance(raw, dict):
        raise ValueError("event must be a JSON object")

    tz_name = raw.get("timezone") or DEFAULT_TIMEZONE
    if not isinstance(tz_name, str) or not tz_name.strip():
        raise ValueError("timezone must be a non-empty string")
    tz_obj = _resolve_tz(tz_name)

    schema_version = raw.get("schema_version", SCHEMA_VERSION)
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version < 1:
        raise ValueError("schema_version must be a positive integer")

    event_type = raw.get("event_type")
    if event_type not in ALLOWED_EVENT_TYPES:
        raise ValueError(
            f"event_type must be one of {sorted(ALLOWED_EVENT_TYPES)}, got {event_type!r}"
        )

    for key in ("app_token", "table_id", "reader_identity"):
        val = raw.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"{key} must be a non-empty string")

    app_token = raw["app_token"].strip()
    if any(sep in app_token for sep in ("/", "\\", "..")) or app_token in (".", ".."):
        raise ValueError(f"app_token must be a safe single path segment, got {app_token!r}")

    # Validate ONLY outer envelope fields; payload is industry-specific and left flexible
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object (dict)")

    ts_raw = raw.get("ts")
    if ts_raw:
        if not isinstance(ts_raw, str):
            raise ValueError("ts must be an ISO 8601 string")
        dt = _parse_iso_ts(ts_raw)
    else:
        dt = datetime.now(tz_obj).replace(microsecond=0)

    event_id = raw.get("event_id")
    if not event_id:
        utc_stamp = dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        event_id = f"evt_{utc_stamp}_{secrets.token_hex(3)}"
    elif not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id must be a non-empty string")

    normalized: Dict[str, Any] = {
        "schema_version": schema_version,
        "event_id": event_id.strip(),
        "event_type": event_type,
        "ts": dt.astimezone(tz_obj).isoformat(),
        "timezone": tz_name.strip(),
        "app_token": raw["app_token"].strip(),
        "table_id": raw["table_id"].strip(),
        "reader_identity": raw["reader_identity"].strip(),
        "payload": payload,
    }
    month_key = dt.astimezone(tz_obj).strftime("%Y-%m")
    return normalized, month_key


class LockFile:
    """Cross-platform exclusive lock file (safe on Windows, macOS, Linux)."""

    def __init__(self, path: Path, timeout_s: float = 10.0, stale_s: float = 60.0):
        self.path = path
        self.timeout_s = timeout_s
        self.stale_s = stale_s
        self._fd: Optional[int] = None

    def __enter__(self) -> "LockFile":
        deadline = time.monotonic() + self.timeout_s
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self._fd = os.open(
                    str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
                meta = f"{os.getpid()} {time.time():.3f}\n".encode("utf-8")
                os.write(self._fd, meta)
                os.close(self._fd)
                self._fd = None
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                    if age > self.stale_s:
                        self.path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out acquiring lock: {self.path}")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def _update_recent_index(root: Path, event: Dict[str, Any]) -> None:
    recent_path = root / "recent.json"
    lock_path = root / ".recent.lock"
    table_name = ""
    if isinstance(event.get("payload"), dict):
        tn = event["payload"].get("table_name")
        if isinstance(tn, str):
            table_name = tn

    entry = {
        "app_token": event["app_token"],
        "table_id": event["table_id"],
        "table_name": table_name,
        "reader_identity": event["reader_identity"],
        "last_ts": event["ts"],
        "last_event_type": event["event_type"],
    }
    try:
        with LockFile(lock_path, timeout_s=5.0):
            items: List[Dict[str, Any]] = []
            if recent_path.exists():
                try:
                    loaded = json.loads(recent_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, list):
                        items = loaded
                except (OSError, json.JSONDecodeError):
                    items = []
            items = [
                it
                for it in items
                if not (
                    it.get("app_token") == entry["app_token"]
                    and it.get("table_id") == entry["table_id"]
                )
            ]
            items.insert(0, entry)
            items = items[:20]
            tmp = recent_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            os.replace(tmp, recent_path)
    except (OSError, TimeoutError):
        pass


def append_event(
    raw_event: Dict[str, Any],
    *,
    ledger_root: Optional[str] = None,
    workspace: Optional[str] = None,
) -> Dict[str, Any]:
    event, month_key = validate_and_normalize_event(raw_event)
    root = resolve_ledger_root(ledger_root, workspace)
    if root is None:
        return {
            "ok": False,
            "ledger_mode": "unavailable",
            "reason": "no_writable_ledger_root",
            "event": event,
        }

    app_dir = root / event["app_token"]
    app_dir.mkdir(parents=True, exist_ok=True)
    target = app_dir / f"{month_key}.jsonl"
    lock_path = app_dir / ".lock"
    line_bytes = (
        json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")

    with LockFile(lock_path):
        fd = os.open(
            str(target),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )
        try:
            written = 0
            while written < len(line_bytes):
                n = os.write(fd, line_bytes[written:])
                if n <= 0:
                    raise OSError("short write while appending ledger event")
                written += n
            os.fsync(fd)
        finally:
            os.close(fd)

    _update_recent_index(root, event)
    return {
        "ok": True,
        "ledger_mode": "writable",
        "path": str(target),
        "event_id": event["event_id"],
        "month": month_key,
    }


def find_latest_event(
    *,
    app_token: str,
    table_id: Optional[str] = None,
    event_type: Optional[str] = None,
    reader_identity: Optional[str] = None,
    ledger_root: Optional[str] = None,
    workspace: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    root = resolve_ledger_root(ledger_root, workspace, require_writable=False)
    if root is None:
        return None
    app_dir = root / app_token
    if not app_dir.is_dir():
        return None

    files = sorted(app_dir.glob("*.jsonl"), reverse=True)
    for fp in files:
        try:
            lines = fp.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if table_id and obj.get("table_id") != table_id:
                continue
            if event_type and obj.get("event_type") != event_type:
                continue
            if reader_identity and obj.get("reader_identity") != reader_identity:
                continue
            return obj
    return None


def main(argv: Optional[List[str]] = None) -> int:
    _ensure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Feishu Controller JSONL ledger helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_loc = sub.add_parser("locate", help="Resolve writable ledger root path")
    p_loc.add_argument("--ledger-root", default=None)
    p_loc.add_argument("--workspace", default=None)

    p_app = sub.add_parser("append", help="Validate outer fields and atomically append event")
    p_app.add_argument("--input", default="-", help="JSON file path or '-' for stdin")
    p_app.add_argument("--ledger-root", default=None)
    p_app.add_argument("--workspace", default=None)

    p_lat = sub.add_parser("latest", help="Find the latest matching event across monthly files")
    p_lat.add_argument("--app-token", required=True)
    p_lat.add_argument("--table-id", default=None)
    p_lat.add_argument("--event-type", choices=sorted(ALLOWED_EVENT_TYPES), default=None)
    p_lat.add_argument("--reader-identity", default=None)
    p_lat.add_argument("--ledger-root", default=None)
    p_lat.add_argument("--workspace", default=None)

    args = parser.parse_args(argv)

    if args.cmd == "locate":
        root = resolve_ledger_root(args.ledger_root, args.workspace)
        out = {
            "ledger_mode": "writable" if root else "unavailable",
            "ledger_root": str(root) if root else None,
        }
        print(json.dumps(out, ensure_ascii=False))
        return 0

    if args.cmd == "append":
        raw_text = (
            sys.stdin.read()
            if args.input == "-"
            else Path(args.input).read_text(encoding="utf-8")
        )
        raw_event = json.loads(raw_text)
        res = append_event(
            raw_event, ledger_root=args.ledger_root, workspace=args.workspace
        )
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 2

    if args.cmd == "latest":
        found = find_latest_event(
            app_token=args.app_token,
            table_id=args.table_id,
            event_type=args.event_type,
            reader_identity=args.reader_identity,
            ledger_root=args.ledger_root,
            workspace=args.workspace,
        )
        print(json.dumps({"found": found is not None, "event": found}, ensure_ascii=False))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
