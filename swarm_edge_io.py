from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple


def merge_notes_blob(value: Any) -> Dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)

    text = str(value).strip()
    if not text:
        return {}

    objects: List[Dict[str, Any]] = []
    for chunk in text.splitlines():
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            obj = json.loads(chunk)
        except Exception:
            continue
        if isinstance(obj, dict):
            objects.append(obj)

    if not objects:
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    merged: Dict[str, Any] = {}
    for obj in objects:
        merged.update(obj)
        resolution = obj.get("resolution")
        if isinstance(resolution, dict):
            merged_resolution = dict(merged.get("resolution") or {})
            merged_resolution.update(resolution)
            merged["resolution"] = merged_resolution
            merged.update(resolution)
    return merged


def load_paper_trades_export(path: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    if not path.exists():
        return {}, []

    payload = json.loads(path.read_text())
    if isinstance(payload, list):
        return {}, [row for row in payload if isinstance(row, dict)]

    if isinstance(payload, dict):
        records = payload.get("records")
        if not isinstance(records, list):
            records = payload.get("trades")
        if not isinstance(records, list):
            records = payload.get("rows")
        record_list = [row for row in records if isinstance(row, dict)] if isinstance(records, list) else []
        meta = {k: v for k, v in payload.items() if k not in {"records", "trades", "rows"}}
        return meta, record_list

    return {}, []
