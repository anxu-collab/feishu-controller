#!/usr/bin/env python3
"""Feishu Bitable deterministic metrics, fingerprinting, normalization & MoM helper.

Optional accelerator script (stdlib-only, Python 3.9+):
- fingerprint: compute schema_fingerprint from field_id + type ONLY (never field_name)
- analyze: normalize Feishu special fields, dedup deterministically, compute aggregations
           (count, sum, avg, group_by), and check strict 5-item comparability against
           a previous briefing_snapshot
- dedup-hash: compute privacy-safe sha256 hashes of dedup keys for writeback audit rows
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore
    ZoneInfoNotFoundError = Exception  # type: ignore

DEFAULT_TIMEZONE = "Asia/Shanghai"


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


def compute_schema_fingerprint(
    fields: Sequence[Dict[str, Any]],
    reader_identity: str = "default",
) -> Dict[str, Any]:
    """Hash field_id + type only so renaming a field never causes false schema drift."""
    pairs: List[Tuple[str, Any]] = []
    for f in fields:
        fid = str(f.get("field_id") or "").strip()
        if not fid:
            raise ValueError(f"Field missing field_id: {f!r}")
        ftype = f.get("type")
        pairs.append((fid, ftype))
    pairs.sort(key=lambda item: item[0])
    canonical = json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return {
        "schema_fingerprint": f"sha256:{digest}",
        "reader_identity": reader_identity,
        "field_count": len(pairs),
    }


def _normalize_timestamp_ms(val: Any, tz_obj) -> Optional[str]:
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)) and abs(val) >= 1_000_000_000_000:
        dt = datetime.fromtimestamp(val / 1000.0, tz=timezone.utc).astimezone(tz_obj)
        return dt.isoformat()
    return None


def normalize_value(val: Any, tz_obj, *, is_time_field: bool = False) -> Any:
    """Normalize Feishu Bitable special field shapes into deterministic primitives."""
    if val is None:
        return None

    # Formula / Lookup wrapper: {"type": N, "value": [...]}
    if isinstance(val, dict) and "value" in val and set(val.keys()) <= {"type", "value"}:
        inner = val["value"]
        if isinstance(inner, list) and len(inner) == 1:
            return normalize_value(inner[0], tz_obj, is_time_field=is_time_field)
        return normalize_value(inner, tz_obj, is_time_field=is_time_field)

    # Hyperlink: {"text": "...", "link": "..."}
    if isinstance(val, dict) and "link" in val:
        return str(val.get("link") or val.get("text") or "").strip()

    # Person / single user object
    if isinstance(val, dict) and ("id" in val or "open_id" in val or "en_name" in val or "name" in val):
        return str(val.get("id") or val.get("open_id") or val.get("name") or "").strip()

    # Rich text segments or list values (multi-select, persons, attachments, lookup arrays)
    if isinstance(val, list):
        if not val:
            return []
        if all(isinstance(x, dict) and "text" in x for x in val):
            return "".join(str(x.get("text", "")) for x in val).strip()
        if all(isinstance(x, dict) and "file_token" in x for x in val):
            return [str(x.get("name") or x.get("file_token")) for x in val]
        norm_items = [normalize_value(x, tz_obj, is_time_field=is_time_field) for x in val]
        norm_items = [x for x in norm_items if x not in (None, "")]
        if all(isinstance(x, str) for x in norm_items):
            return sorted(norm_items)
        return norm_items

    if isinstance(val, str):
        return val.strip()

    if is_time_field:
        ts_iso = _normalize_timestamp_ms(val, tz_obj)
        if ts_iso is not None:
            return ts_iso

    return val


def _to_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return None if math.isnan(val) or math.isinf(val) else float(val)
    if isinstance(val, str):
        cleaned = val.replace(",", "").strip()
        if not cleaned:
            return None
        try:
            num = float(cleaned)
            return None if math.isnan(num) or math.isinf(num) else num
        except ValueError:
            return None
    return None


def _key_part(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, list):
        return "|".join(str(x) for x in val)
    return str(val).strip()


def hash_dedup_key(fields: Dict[str, Any], dedup_keys: Sequence[str], tz_name: str = DEFAULT_TIMEZONE) -> str:
    tz_obj = _resolve_tz(tz_name)
    parts = [_key_part(normalize_value(fields.get(k), tz_obj)) for k in dedup_keys]
    raw = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def dedup_records(
    records: Sequence[Dict[str, Any]],
    dedup_keys: Sequence[str],
    *,
    time_field: Optional[str] = None,
    tz_name: str = DEFAULT_TIMEZONE,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Deterministically normalize and deduplicate records.

    If dedup_keys is empty, deduplicates by record_id only.
    On collision, keeps the record with the newer time_field (or later row if absent).
    """
    tz_obj = _resolve_tz(tz_name)
    by_key: Dict[str, Tuple[str, int, Dict[str, Any]]] = {}
    duplicates: List[Dict[str, Any]] = []

    for idx, rec in enumerate(records):
        rid = str(rec.get("record_id") or f"__row_{idx}")
        raw_fields = rec.get("fields") if isinstance(rec.get("fields"), dict) else {}
        norm_fields = {
            k: normalize_value(v, tz_obj, is_time_field=(k == time_field))
            for k, v in raw_fields.items()
        }
        entry = {"record_id": rid, "fields": norm_fields}

        if dedup_keys:
            parts = [_key_part(norm_fields.get(k)) for k in dedup_keys]
            if all(p == "" for p in parts):
                dkey = f"__rid__:{rid}"
            else:
                dkey = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
        else:
            dkey = f"__rid__:{rid}"

        t_val = str(norm_fields.get(time_field) or "") if time_field else ""

        if dkey in by_key:
            prev_t, prev_idx, prev_entry = by_key[dkey]
            # Keep newer time_field; tie-break by later index
            if (t_val, idx) >= (prev_t, prev_idx):
                duplicates.append(
                    {"dropped_record_id": prev_entry["record_id"], "kept_record_id": rid}
                )
                by_key[dkey] = (t_val, idx, entry)
            else:
                duplicates.append(
                    {"dropped_record_id": rid, "kept_record_id": prev_entry["record_id"]}
                )
        else:
            by_key[dkey] = (t_val, idx, entry)

    ordered = [item[2] for item in sorted(by_key.values(), key=lambda x: x[1])]
    return ordered, duplicates


def _norm_opt(val: Any) -> Any:
    if val == "":
        return None
    return val


def check_comparability(
    current_ctx: Dict[str, Any],
    prev_snapshot: Optional[Dict[str, Any]],
) -> Tuple[bool, List[str]]:
    """Strict 5-item + reader_identity comparability gate."""
    if not prev_snapshot:
        return False, ["no_previous_snapshot"]

    prev_payload = prev_snapshot.get("payload") if isinstance(prev_snapshot.get("payload"), dict) else {}
    mismatches: List[str] = []

    checks = [
        ("table_id", _norm_opt(current_ctx.get("table_id")), _norm_opt(prev_snapshot.get("table_id"))),
        ("view_id", _norm_opt(current_ctx.get("view_id")), _norm_opt(prev_payload.get("view_id"))),
        ("filter_signature", _norm_opt(current_ctx.get("filter_signature")), _norm_opt(prev_payload.get("filter_signature"))),
        ("lens_version", current_ctx.get("lens_version"), prev_payload.get("lens_version")),
        ("schema_fingerprint", _norm_opt(current_ctx.get("schema_fingerprint")), _norm_opt(prev_payload.get("schema_fingerprint"))),
        ("reader_identity", _norm_opt(current_ctx.get("reader_identity")), _norm_opt(prev_snapshot.get("reader_identity"))),
    ]
    for name, cur_val, prev_val in checks:
        if cur_val != prev_val:
            mismatches.append(f"{name}: {prev_val!r} -> {cur_val!r}")

    return len(mismatches) == 0, mismatches


def compute_metrics(
    records: Sequence[Dict[str, Any]],
    *,
    group_by: Sequence[str] = (),
    sum_fields: Sequence[str] = (),
    avg_fields: Sequence[str] = (),
) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {"row_count": len(records)}

    for sf in sum_fields:
        vals = [_to_float(r["fields"].get(sf)) for r in records]
        nums = [v for v in vals if v is not None]
        metrics[f"sum:{sf}"] = round(sum(nums), 6)

    for af in avg_fields:
        vals = [_to_float(r["fields"].get(af)) for r in records]
        nums = [v for v in vals if v is not None]
        metrics[f"avg:{af}"] = round(sum(nums) / len(nums), 6) if nums else None

    breakdowns: Dict[str, Dict[str, int]] = {}
    for gf in group_by:
        counter: Counter = Counter()
        for r in records:
            v = r["fields"].get(gf)
            if isinstance(v, list):
                if not v:
                    counter["(空)"] += 1
                else:
                    for item in v:
                        counter[str(item)] += 1
            elif v in (None, ""):
                counter["(空)"] += 1
            else:
                counter[str(v)] += 1
        breakdowns[gf] = dict(counter.most_common())

    return {"metrics": metrics, "breakdowns": breakdowns}


def compute_mom(
    cur_metrics: Dict[str, Any],
    prev_metrics: Dict[str, Any],
) -> Dict[str, Any]:
    deltas: Dict[str, Any] = {}
    for k, cur_v in cur_metrics.items():
        if not isinstance(cur_v, (int, float)) or isinstance(cur_v, bool):
            continue
        prev_v = prev_metrics.get(k)
        if not isinstance(prev_v, (int, float)) or isinstance(prev_v, bool):
            continue
        diff = round(cur_v - prev_v, 6)
        pct = round((diff / prev_v) * 100.0, 2) if prev_v != 0 else None
        deltas[k] = {"current": cur_v, "previous": prev_v, "delta": diff, "pct": pct}
    return deltas


def _read_json(path: str) -> Any:
    if path == "-":
        return json.loads(sys.stdin.read())
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def main(argv: Optional[List[str]] = None) -> int:
    _ensure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Feishu Bitable deterministic metrics helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fp = sub.add_parser("fingerprint", help="Compute schema_fingerprint from field_id + type")
    p_fp.add_argument("--fields", required=True, help="JSON file of fields array or '-'")
    p_fp.add_argument("--reader-identity", default="default")

    p_an = sub.add_parser("analyze", help="Normalize, dedup, aggregate, and compute strict MoM")
    p_an.add_argument("--records", required=True, help="JSON file of records array or '-'")
    p_an.add_argument("--dedup-keys", default="", help="Comma-separated dedup key field names")
    p_an.add_argument("--time-field", default=None)
    p_an.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    p_an.add_argument("--group-by", default="", help="Comma-separated fields to group count")
    p_an.add_argument("--sum-fields", default="", help="Comma-separated numeric fields to sum")
    p_an.add_argument("--avg-fields", default="", help="Comma-separated numeric fields to average")
    p_an.add_argument("--context", default=None, help="JSON file with current 5-item context")
    p_an.add_argument("--prev-snapshot", default=None, help="JSON file of previous briefing_snapshot event")

    p_dh = sub.add_parser("dedup-hash", help="Hash dedup key fields for writeback audit rows")
    p_dh.add_argument("--rows", required=True, help="JSON file of row field dicts or '-'")
    p_dh.add_argument("--dedup-keys", required=True, help="Comma-separated dedup key field names")
    p_dh.add_argument("--timezone", default=DEFAULT_TIMEZONE)

    args = parser.parse_args(argv)

    if args.cmd == "fingerprint":
        fields = _read_json(args.fields)
        if isinstance(fields, dict) and "items" in fields:
            fields = fields["items"]
        print(json.dumps(compute_schema_fingerprint(fields, args.reader_identity), ensure_ascii=False))
        return 0

    if args.cmd == "analyze":
        records = _read_json(args.records)
        if isinstance(records, dict) and "items" in records:
            records = records["items"]
        dedup_keys = [k.strip() for k in args.dedup_keys.split(",") if k.strip()]
        group_by = [k.strip() for k in args.group_by.split(",") if k.strip()]
        sum_fields = [k.strip() for k in args.sum_fields.split(",") if k.strip()]
        avg_fields = [k.strip() for k in args.avg_fields.split(",") if k.strip()]

        deduped, dups = dedup_records(
            records, dedup_keys, time_field=args.time_field, tz_name=args.timezone
        )
        agg = compute_metrics(
            deduped, group_by=group_by, sum_fields=sum_fields, avg_fields=avg_fields
        )

        comparable = False
        incomparable_reasons = ["no_previous_snapshot"]
        mom = None
        prev_ts = None
        if args.context and args.prev_snapshot:
            ctx = _read_json(args.context)
            prev = _read_json(args.prev_snapshot)
            comparable, incomparable_reasons = check_comparability(ctx, prev)
            prev_ts = prev.get("ts") if isinstance(prev, dict) else None
            if comparable:
                prev_metrics = (prev.get("payload") or {}).get("metrics") or {}
                mom = compute_mom(agg["metrics"], prev_metrics)

        out = {
            "script_verified": True,
            "row_count_raw": len(records),
            "row_count_deduped": len(deduped),
            "duplicate_count": len(dups),
            "duplicates": dups[:50],
            "metrics": agg["metrics"],
            "breakdowns": agg["breakdowns"],
            "comparable_to_prev": comparable,
            "incomparable_reasons": incomparable_reasons,
            "prev_snapshot_ts": prev_ts,
            "mom": mom,
            "cited_record_ids": [r["record_id"] for r in deduped[:200]],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "dedup-hash":
        rows = _read_json(args.rows)
        keys = [k.strip() for k in args.dedup_keys.split(",") if k.strip()]
        hashes = [
            {"index": i, "dedup_hash": hash_dedup_key(r.get("fields", r), keys, args.timezone)}
            for i, r in enumerate(rows)
        ]
        print(json.dumps(hashes, ensure_ascii=False))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
