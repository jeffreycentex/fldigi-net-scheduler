#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 jeffreycentex
"""Export the FLDigi Net Scheduler nets to CSV, or import them back.

Stand-alone command-line tool (no Qt needed). Close the GUI before
importing: it autosaves when it closes and would overwrite the import.

  python3 nets_csv.py export [-o nets.csv] [--config schedule.json] [--no-passwords]
  python3 nets_csv.py import nets.csv [--config schedule.json] [--append] [--dry-run]

Import replaces all nets by default (settings, the Default SDR and a running
net are kept). --append adds the CSV rows after the existing nets.
A backup (schedule.json.bak-YYYYmmdd-HHMMSS) is written before any change.

CSV columns (header row required, column order doesn't matter):
  enabled        On / Off   (also yes/no, true/false, 1/0)
  name           net name / label
  day            Daily, Weekdays, Weekends, Mon, Tue, Wed, Thu, Fri, Sat, Sun (UTC)
  start_utc      HH:MM 24 h UTC  (also 0100, 1:00, 1:00:00, 1:00 AM)
  freq_khz       dial frequency in kHz, blank = don't change
  rig_mode       USB, LSB, ...  blank or "(no change)" = don't change
  digital_mode   fldigi modem name, blank or "(no change)" = don't change
  sweet_spot_hz  the net's sweet spot in Hz (default 1500)
  sdr            Off / Default / Custom
  sdr_server     host:port, used when sdr = Custom
  sdr_password   optional, used when sdr = Custom
  record         On / Off
  duration_min   minutes; required when sdr or record is used
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import re
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sched_core as core  # noqa: E402
from sched_core import DAYS, NO_CHANGE, Entry  # noqa: E402

COLUMNS = ["enabled", "name", "day", "start_utc", "freq_khz", "rig_mode", "digital_mode",
           "sweet_spot_hz", "sdr", "sdr_server", "sdr_password", "record", "duration_min"]

_TRUE = {"on", "yes", "y", "true", "1", "x"}
_FALSE = {"off", "no", "n", "false", "0", ""}


def _bool(v: str, col: str) -> bool:
    s = (v or "").strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ValueError(f"{col}: '{v}' is not On/Off")


def _time(v: str) -> str:
    """Accept what Excel tends to write back: 1:00, 01:00, 1:00:00, 1:00 AM, 0100."""
    s = (v or "").strip()
    if not s:
        return ""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?", s)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
        if ap:
            if not 1 <= h <= 12:
                raise ValueError(f"start_utc: '{v}' is not a valid time")
            h = h % 12 + (12 if ap.lower() == "pm" else 0)
        s = f"{h:02d}:{mi:02d}"
    t = core.normalize_time(s)
    if t is None:
        raise ValueError(f"start_utc: '{v}' is not HH:MM (24 h UTC)")
    return t


def _int(v: str, col: str, default: int) -> int:
    s = (v or "").strip()
    if not s:
        return default
    try:
        return int(float(s))
    except ValueError:
        raise ValueError(f"{col}: '{v}' is not a number") from None


_DAY_ALIASES = {d.lower(): d for d in DAYS}
_DAY_ALIASES.update({"monday": "Mon", "tuesday": "Tue", "wednesday": "Wed", "thursday": "Thu",
                     "friday": "Fri", "saturday": "Sat", "sunday": "Sun"})


def _day(v: str) -> str:
    d = _DAY_ALIASES.get((v or "").strip().lower())
    if d is None:
        raise ValueError(f"day: '{v}' is not one of {', '.join(DAYS)}")
    return d


def row_to_entry(row: dict) -> Entry:
    g = lambda k: (row.get(k) or "").strip()
    sdr = g("sdr").lower() or "off"
    if sdr not in core.SDR_MODES:
        raise ValueError(f"sdr: '{row.get('sdr')}' is not Off/Default/Custom")
    freq = g("freq_khz").replace(",", ".")
    if freq and core.parse_freq_khz(freq) is None:
        raise ValueError(f"freq_khz: '{row.get('freq_khz')}' is not a number in kHz")
    return Entry(
        enabled=_bool(g("enabled"), "enabled"),
        name=g("name"),
        day=_day(g("day") or "Mon"),
        start_utc=_time(g("start_utc")),
        freq_khz=freq,
        rig_mode=g("rig_mode") or NO_CHANGE,
        digital_mode=g("digital_mode") or NO_CHANGE,
        sweet_spot_hz=_int(g("sweet_spot_hz"), "sweet_spot_hz", core.DEFAULT_SWEET_BASE),
        sdr_mode=sdr,
        sdr_server=g("sdr_server"),
        sdr_password=row.get("sdr_password") or "",
        record=_bool(g("record"), "record"),
        duration_min=_int(g("duration_min"), "duration_min", core.DEFAULT_DURATION_MIN),
    )


def entry_to_row(e: Entry, passwords: bool) -> dict:
    return {
        "enabled": "On" if e.enabled else "Off",
        "name": e.name, "day": e.day, "start_utc": e.start_utc, "freq_khz": e.freq_khz,
        "rig_mode": e.rig_mode, "digital_mode": e.digital_mode,
        "sweet_spot_hz": e.sweet_spot_hz,
        "sdr": e.sdr_mode.capitalize(), "sdr_server": e.sdr_server,
        "sdr_password": e.sdr_password if passwords else "",
        "record": "On" if e.record else "Off", "duration_min": e.duration_min,
    }


def load(path: str) -> core.Document:
    if not os.path.exists(path):
        print(f"No schedule at {path}; starting an empty one.")
        return core.Document()
    return core.load_document(path)


def cmd_export(a) -> int:
    doc = load(a.config)
    nets = [e for e in doc.entries if not e.is_blank()]
    out = a.output
    # utf-8-sig writes a BOM so Excel opens non-ASCII names correctly
    fh = sys.stdout if out == "-" else open(out, "w", newline="", encoding="utf-8-sig")
    try:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for e in nets:
            w.writerow(entry_to_row(e, not a.no_passwords))
    finally:
        if fh is not sys.stdout:
            fh.close()
    if out != "-":
        print(f"Exported {len(nets)} net(s) from {a.config} to {out}")
    return 0


def read_csv(path: str) -> list[tuple[int, dict]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        text = fh.read()
    # Only the delimiter is guessed (Excel uses ";" in some locales);
    # quoting stays standard Excel CSV.
    header = text.split("\n", 1)[0]
    delim = max((",", ";", "\t"), key=header.count)
    rd = csv.DictReader(io.StringIO(text, newline=""), dialect=csv.excel, delimiter=delim)
    if not rd.fieldnames:
        raise SystemExit(f"{path}: empty file")
    rd.fieldnames = [f.strip().lower() for f in rd.fieldnames]
    unknown = [f for f in rd.fieldnames if f and f not in COLUMNS]
    if unknown:
        print(f"Ignoring unknown column(s): {', '.join(unknown)}")
    if "start_utc" not in rd.fieldnames:
        raise SystemExit(f"{path}: needs at least a start_utc column (header row: {', '.join(COLUMNS)})")
    rows = []
    for n, row in enumerate(rd, start=2):          # line 1 is the header
        if not any((v or "").strip() for k, v in row.items() if k):
            continue                                # skip empty lines
        rows.append((n, row))
    return rows


def cmd_import(a) -> int:
    rows = read_csv(a.csv)
    doc = load(a.config)
    errors, new = [], []
    for line, row in rows:
        try:
            new.append(row_to_entry(row))
        except ValueError as ex:
            errors.append(f"  line {line}: {ex}")
    if errors:
        print(f"Not imported - {len(errors)} error(s) in {a.csv}:")
        print("\n".join(errors))
        return 1
    warnings = []
    for i, e in enumerate(new):
        p = e.problems(doc.settings)
        if p:
            warnings.append(f"  {e.label(i)}: " + "; ".join(p))
    entries = (list(doc.entries) + new) if a.append else new
    print(f"{'Would import' if a.dry_run else 'Importing'} {len(new)} net(s) "
          f"({'appended to' if a.append else 'replacing'} {len(doc.entries)} existing).")
    if warnings:
        print("These nets will show red in the GUI and won't run until fixed:")
        print("\n".join(warnings))
    if a.dry_run:
        return 0
    if os.path.exists(a.config):
        bak = f"{a.config}.bak-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(a.config, bak)
        print(f"Backup: {bak}")
    doc.entries = entries
    core.save_document(doc, a.config)
    print(f"Saved {a.config}. Start the GUI to see the nets.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Export / import FLDigi Net Scheduler nets as CSV. "
                                             "Close the GUI before importing.")
    ap.add_argument("--config", default=core.default_config_path(),
                    help="schedule JSON (default: %(default)s)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export", help="write the nets to CSV")
    ex.add_argument("-o", "--output", default="nets.csv", help="CSV file, or - for stdout (default: nets.csv)")
    ex.add_argument("--no-passwords", action="store_true", help="leave sdr_password empty")
    im = sub.add_parser("import", help="load nets from CSV into the schedule")
    im.add_argument("csv", help="CSV file to import")
    im.add_argument("--append", action="store_true", help="add after the existing nets instead of replacing")
    im.add_argument("--dry-run", action="store_true", help="check the CSV and report, don't save")
    for p in (ex, im):      # allow --config after the sub-command too
        p.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    a.config = os.path.abspath(os.path.expanduser(a.config))
    return cmd_export(a) if a.cmd == "export" else cmd_import(a)


if __name__ == "__main__":
    sys.exit(main())
