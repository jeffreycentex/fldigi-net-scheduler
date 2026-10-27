# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 jeffreycentex
"""nets_csv.py: parsing of Excel-style values and the export/import commands."""
from __future__ import annotations

import dataclasses
import glob
import os

import pytest

import nets_csv
import sched_core as core
from sched_core import Entry, Settings

HEADER = ",".join(nets_csv.COLUMNS)


def test_columns_cover_every_entry_field():
    """CLAUDE.md: keep nets_csv in sync when Entry gains fields."""
    fields = {f.name for f in dataclasses.fields(Entry)}
    cols = {"sdr_mode" if c == "sdr" else c for c in nets_csv.COLUMNS}
    assert cols == fields


@pytest.mark.parametrize("v,want", [
    ("01:00", "01:00"), ("1:00", "01:00"), ("0100", "01:00"), ("1:00:00", "01:00"),
    ("1:00 AM", "01:00"), ("12:30 am", "00:30"), ("12:30 PM", "12:30"), ("1:05pm", "13:05"),
    ("23:59", "23:59"), ("", ""),
])
def test_time(v, want):
    assert nets_csv._time(v) == want


@pytest.mark.parametrize("v", ["13:00 PM", "0:30 AM", "24:00", "noon", "1.00"])
def test_time_rejects(v):
    with pytest.raises(ValueError, match="start_utc"):
        nets_csv._time(v)


@pytest.mark.parametrize("v,want", [("On", True), ("YES", True), ("x", True), ("1", True),
                                    ("off", False), ("", False), ("False", False)])
def test_bool(v, want):
    assert nets_csv._bool(v, "enabled") is want


def test_bool_rejects():
    with pytest.raises(ValueError, match="record"):
        nets_csv._bool("maybe", "record")


@pytest.mark.parametrize("v,want", [("mon", "Mon"), ("Monday", "Mon"), (" SUNDAY ", "Sun"),
                                    ("daily", "Daily"), ("weekends", "Weekends")])
def test_day(v, want):
    assert nets_csv._day(v) == want


def test_day_rejects():
    with pytest.raises(ValueError, match="day"):
        nets_csv._day("Someday")


def test_int():
    assert nets_csv._int("", "x", 7) == 7
    assert nets_csv._int("60.0", "x", 7) == 60
    with pytest.raises(ValueError, match="duration_min"):
        nets_csv._int("an hour", "duration_min", 7)


def test_row_to_entry_defaults():
    e = nets_csv.row_to_entry({"start_utc": "1:00"})
    assert e == Entry(enabled=False, day="Mon", start_utc="01:00", rig_mode=core.NO_CHANGE,
                      digital_mode=core.NO_CHANGE)


def test_row_to_entry_full():
    e = nets_csv.row_to_entry({
        "enabled": "On", "name": " Net ", "day": "Tuesday", "start_utc": "0100",
        "freq_khz": "3595,5", "rig_mode": "USB", "digital_mode": "Cont-4/250",
        "sweet_spot_hz": "900", "sdr": "Custom", "sdr_server": "k:8073",
        "sdr_password": " pw ", "record": "On", "duration_min": "90"})
    assert e == Entry(enabled=True, name="Net", day="Tue", start_utc="01:00", freq_khz="3595.5",
                      rig_mode="USB", digital_mode="Cont-4/250", sweet_spot_hz=900,
                      sdr_mode="custom", sdr_server="k:8073", sdr_password=" pw ",
                      record=True, duration_min=90)


@pytest.mark.parametrize("row,col", [({"sdr": "maybe"}, "sdr"), ({"freq_khz": "abc"}, "freq_khz")])
def test_row_to_entry_rejects(row, col):
    with pytest.raises(ValueError, match=col):
        nets_csv.row_to_entry(dict(row, start_utc="01:00"))


# ---------------------------------------------------------------- commands

NETS = [
    Entry(enabled=True, name="Préppernet Digital", day="Mon", start_utc="01:00", freq_khz="3595",
          rig_mode="USB", digital_mode="Cont-4/250", sweet_spot_hz=900),
    Entry(enabled=False, name="AIB, 40m", day="Daily", start_utc="23:58", freq_khz="850",
          rig_mode="AM", sdr_mode="custom", sdr_server="k:8073", sdr_password="pw",
          record=True, duration_min=1),
]


@pytest.fixture
def config(tmp_path):
    path = str(tmp_path / "schedule.json")
    st = Settings(port=17362, default_sdr_server="d:8073",
                  active_net={"label": "running", "started_utc": "", "end_utc": ""})
    core.save_document(core.Document(st, list(NETS)), path)
    return path


def run(*argv) -> int:
    return nets_csv.main(list(argv))


def test_export_import_round_trip(config, tmp_path, capsys):
    out = str(tmp_path / "nets.csv")
    assert run("--config", config, "export", "-o", out) == 0
    raw = open(out, "rb").read()
    assert raw.startswith(b"\xef\xbb\xbf")        # BOM for Excel
    assert b"pw" in raw

    other = str(tmp_path / "other.json")
    assert run("import", out, "--config", other) == 0
    doc = core.load_document(other)
    assert doc.entries == NETS


def test_import_replaces_keeps_settings_and_backs_up(config, tmp_path, capsys):
    csv_path = tmp_path / "new.csv"
    csv_path.write_text(HEADER + "\nOn,New Net,Fri,1:00 PM,7110,LSB,,1500,Off,,,Off,\n",
                        encoding="utf-8")
    assert run("--config", config, "import", str(csv_path)) == 0
    doc = core.load_document(config)
    assert [e.name for e in doc.entries] == ["New Net"]
    assert doc.entries[0].start_utc == "13:00" and doc.entries[0].day == "Fri"
    assert doc.settings.port == 17362 and doc.settings.active_net["label"] == "running"
    baks = glob.glob(config + ".bak-*")
    assert len(baks) == 1
    assert core.load_document(baks[0]).entries == NETS


def test_import_append(config, tmp_path):
    csv_path = tmp_path / "new.csv"
    csv_path.write_text("start_utc,name\n02:00,Extra\n", encoding="utf-8")
    assert run("--config", config, "import", str(csv_path), "--append") == 0
    assert [e.name for e in core.load_document(config).entries] == \
        [NETS[0].name, NETS[1].name, "Extra"]


def test_import_dry_run_changes_nothing(config, tmp_path, capsys):
    before = open(config, "rb").read()
    csv_path = tmp_path / "new.csv"
    csv_path.write_text("start_utc,name,sdr\n02:00,Extra,Default\n", encoding="utf-8")
    assert run("--config", config, "import", str(csv_path), "--dry-run") == 0
    assert open(config, "rb").read() == before
    assert not glob.glob(config + ".bak-*")
    assert "Would import 1" in capsys.readouterr().out


def test_import_errors_change_nothing(config, tmp_path, capsys):
    before = open(config, "rb").read()
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("start_utc,day\n01:00,Mon\n25:00,Mon\n01:00,Someday\n", encoding="utf-8")
    assert run("--config", config, "import", str(csv_path)) == 1
    out = capsys.readouterr().out
    assert "2 error(s)" in out and "line 3" in out and "line 4" in out
    assert open(config, "rb").read() == before


def test_import_semicolon_bom_and_blank_lines(tmp_path):
    csv_path = tmp_path / "excel.csv"
    csv_path.write_bytes("﻿Enabled;Name;Start_UTC;Freq_kHz;Comment\n"
                         "yes;Net A;01:00;7110,5;hi\n;;;;\nno;Net B;1:00:00;;\n".encode("utf-8"))
    cfg = str(tmp_path / "s.json")
    assert run("--config", cfg, "import", str(csv_path)) == 0
    doc = core.load_document(cfg)
    assert [(e.enabled, e.name, e.start_utc, e.freq_khz) for e in doc.entries] == \
        [(True, "Net A", "01:00", "7110.5"), (False, "Net B", "01:00", "")]


def test_import_needs_start_utc_column(tmp_path):
    csv_path = tmp_path / "x.csv"
    csv_path.write_text("name\nNet\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="start_utc"):
        run("--config", str(tmp_path / "s.json"), "import", str(csv_path))


def test_export_no_passwords(config, capsys):
    assert run("--config", config, "export", "-o", "-", "--no-passwords") == 0
    out = capsys.readouterr().out
    assert "k:8073" in out and ",pw," not in out


def test_export_missing_config(tmp_path, capsys):
    out = str(tmp_path / "n.csv")
    assert run("--config", str(tmp_path / "missing.json"), "export", "-o", out) == 0
    assert open(out, encoding="utf-8-sig").read().strip() == HEADER
    assert not os.path.exists(tmp_path / "missing.json")
