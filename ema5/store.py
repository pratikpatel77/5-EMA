"""Settings and tracked entries, in one small JSON file.

An entry is keyed by symbol AND alert month, so when a stock prints a fresh
alert candle it comes back as a new candidate even if you ignored the
previous one. Nothing here places an order: `taken` means "track this".
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

VERSION = 1

DEFAULTS = {
    "source": "definedge",        # bulk daily history (fast, automatic login)
    "top_n": 1500,                # top N by market cap
    "history_days": 2600,
    "buffer_pct": 0.1,            # entry buffer above the alert candle high
    "max_distance_pct": 2.0,      # the watch band: 0% .. this, below the trigger
    "approach_pct": 6.0,          # second, wider band shown as "approaching"
    "max_de": 0.25,               # debt-to-equity ceiling for a candidate
    "low_debt_only": True,        # hide rows that fail the debt test
    "debt_ttl_days": 7.0,
    "auto_scan_minutes": 0,       # 0 = manual scans only
}

STATUS_TAKEN = "taken"
STATUS_IGNORED = "ignored"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def entry_id(symbol: str, alert_month: str) -> str:
    return f"{symbol.upper()}:{alert_month}"


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = {"version": VERSION, "settings": {}, "entries": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text("utf-8"))
                if isinstance(loaded, dict):
                    self._data.update({k: loaded.get(k, v) for k, v in self._data.items()})
            except (OSError, ValueError):
                pass

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=1), "utf-8")
        tmp.replace(self.path)

    # -- settings ---------------------------------------------------------- #
    def settings(self) -> dict:
        with self._lock:
            return {**DEFAULTS, **self._data.get("settings", {})}

    def save_settings(self, changes: dict) -> dict:
        clean = {}
        for key, value in (changes or {}).items():
            if key not in DEFAULTS:
                continue
            default = DEFAULTS[key]
            try:
                if isinstance(default, bool):
                    clean[key] = bool(value)
                elif isinstance(default, int):
                    clean[key] = int(value)
                elif isinstance(default, float):
                    clean[key] = float(value)
                else:
                    clean[key] = str(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key} is not a valid value.") from None
        bounds = {"top_n": (50, 3000), "history_days": (400, 8000), "buffer_pct": (0.0, 2.0),
                  "max_distance_pct": (0.1, 25.0), "approach_pct": (0.2, 40.0),
                  "max_de": (0.0, 5.0), "debt_ttl_days": (0.5, 90.0),
                  "auto_scan_minutes": (0, 240)}
        for key, (low, high) in bounds.items():
            if key in clean and not low <= clean[key] <= high:
                raise ValueError(f"{key} must be between {low} and {high}.")
        if clean.get("source") not in (None, "definedge", "fyers"):
            raise ValueError("source must be definedge or fyers.")
        with self._lock:
            self._data.setdefault("settings", {}).update(clean)
            self._save()
            return self.settings()

    # -- entries ----------------------------------------------------------- #
    def entries(self) -> dict[str, dict]:
        with self._lock:
            return json.loads(json.dumps(self._data.get("entries", {})))

    def get(self, key: str) -> dict | None:
        with self._lock:
            found = self._data.get("entries", {}).get(key)
            return dict(found) if found else None

    def put(self, record: dict) -> dict:
        with self._lock:
            record = dict(record)
            record["updated_at"] = now_iso()
            record.setdefault("created_at", record["updated_at"])
            self._data.setdefault("entries", {})[record["id"]] = record
            self._save()
            return record

    def patch(self, key: str, changes: dict) -> dict:
        with self._lock:
            record = self._data.get("entries", {}).get(key)
            if not record:
                raise KeyError(f"{key} is not being tracked.")
            record.update(changes)
            record["updated_at"] = now_iso()
            self._save()
            return dict(record)

    def delete(self, key: str) -> bool:
        with self._lock:
            gone = self._data.get("entries", {}).pop(key, None) is not None
            if gone:
                self._save()
            return gone

    def taken(self) -> list[dict]:
        return [e for e in self.entries().values() if e.get("status") == STATUS_TAKEN]

    def ignored_ids(self) -> set[str]:
        return {k for k, e in self.entries().items() if e.get("status") == STATUS_IGNORED}
