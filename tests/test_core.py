# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 jeffreycentex
"""Pure logic in sched_core: validation, model, persistence, time math, tuning."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import sched_core as core
from sched_core import Entry, Settings

UTC = timezone.utc
MON = datetime(2024, 1, 1, tzinfo=UTC)          # 2024-01-01 was a Monday


def at(day_offset: int, hh: int, mm: int, ss: int = 0) -> datetime:
    return MON + timedelta(days=day_offset, hours=hh, minutes=mm, seconds=ss)


def good_entry(**kw) -> Entry:
    d = dict(enabled=True, name="Test Net", day="Mon", start_utc="01:00", freq_khz="7110")
    d.update(kw)
    return Entry(**d)


# ---------------------------------------------------------------- validation

@pytest.mark.parametrize("s,want", [
    ("01:00", "01:00"), ("1:00", "01:00"), ("0100", "01:00"), (" 23:59 ", "23:59"),
    ("100", "01:00"), ("24:00", None), ("12:60", None), ("", None), (None, None),
    ("1:0", None), ("ab:cd", None), ("01:00:00", None),
])
def test_normalize_time(s, want):
    assert core.normalize_time(s) == want


@pytest.mark.parametrize("s,want", [
    ("7110", 7110.0), ("14070.5", 14070.5), ("14070,5", 14070.5), (" 3595 ", 3595.0),
    ("", None), (None, None), ("abc", None), ("0", None), ("-5", None), ("1e9", None),
])
def test_parse_freq_khz(s, want):
    assert core.parse_freq_khz(s) == want


@pytest.mark.parametrize("day,weekday,want", [
    ("Daily", 0, True), ("Daily", 6, True),
    ("Weekdays", 4, True), ("Weekdays", 5, False),
    ("Weekends", 5, True), ("Weekends", 6, True), ("Weekends", 0, False),
    ("Mon", 0, True), ("Mon", 1, False), ("Sun", 6, True), ("Wed", 2, True),
    ("Funday", 0, False),
])
def test_day_matches(day, weekday, want):
    assert core.day_matches(day, weekday) is want


# ---------------------------------------------------------------- Entry

def test_new_entry_defaults_off_and_blank():
    e = Entry()
    assert not e.enabled and e.is_blank() and e.sdr_mode == "off" and not e.record
    assert not e.is_schedulable()


def test_good_entry_is_schedulable():
    e = good_entry()
    assert e.problems() == []
    assert e.is_schedulable()
    assert not good_entry(enabled=False).is_schedulable()


@pytest.mark.parametrize("kw,fragment", [
    (dict(start_utc="25:00"), "start time"),
    (dict(freq_khz="seven"), "frequency"),
    (dict(day="Someday"), "unknown day"),
    (dict(sweet_spot_hz=50), "sweet spot"),
    (dict(sweet_spot_hz=4000), "sweet spot"),
    (dict(sdr_mode="bogus"), "unknown SDR mode"),
    (dict(sdr_mode="custom", sdr_server=" "), "no server"),
    (dict(record=True, duration_min=0), "duration"),
    (dict(sdr_mode="custom", sdr_server="h:8073", duration_min=core.MAX_DURATION_MIN + 1),
     "duration"),
])
def test_entry_problems(kw, fragment):
    probs = good_entry(**kw).problems()
    assert any(fragment in p for p in probs), probs
    assert not good_entry(**kw).is_schedulable()


def test_blank_freq_is_allowed():
    assert good_entry(freq_khz="").problems() == []


def test_duration_only_checked_when_something_must_end():
    assert good_entry(duration_min=0).problems() == []


def test_default_sdr_needs_configured_server():
    e = good_entry(sdr_mode="default")
    assert e.problems() == []                    # no settings: can't tell yet
    assert any("Default" in p for p in e.problems(Settings()))
    assert e.problems(Settings(default_sdr_server="kiwi.example:8073")) == []


def test_sdr_target():
    st = Settings(default_sdr_server=" kiwi.example:8073 ", default_sdr_password="pw")
    assert good_entry().sdr_target(st) is None
    assert good_entry(sdr_mode="default").sdr_target(st) == ("kiwi.example:8073", "pw")
    assert good_entry(sdr_mode="default").sdr_target(Settings()) is None
    assert good_entry(sdr_mode="custom", sdr_server=" c:1 ", sdr_password="x").sdr_target(st) \
        == ("c:1", "x")
    assert good_entry(sdr_mode="custom").sdr_target(st) is None


def test_needs_end():
    assert not good_entry().needs_end()
    assert good_entry(record=True).needs_end()
    assert good_entry(sdr_mode="default").needs_end()


def test_label():
    assert good_entry(name="  Net  ").label(3) == "Net"
    assert good_entry(name="").label(3) == "row 4"


def test_entry_from_dict_migrates_schema1_offset():
    e = Entry.from_dict({"name": "x", "sweet_spot_offset_hz": -600})
    assert e.sweet_spot_hz == 900


def test_entry_from_dict_coerces_bad_values():
    e = Entry.from_dict({"enabled": 1, "freq_khz": 7110, "sweet_spot_hz": "junk",
                         "sdr_mode": "CUSTOM", "sdr_server": None, "duration_min": "x",
                         "record": "", "unknown_key": 5})
    assert e.enabled is True
    assert e.freq_khz == "7110"
    assert e.sweet_spot_hz == core.DEFAULT_SWEET_BASE
    assert e.sdr_mode == "custom" and e.sdr_server == ""
    assert e.duration_min == core.DEFAULT_DURATION_MIN
    assert e.record is False
    assert Entry.from_dict({"sdr_mode": "nope"}).sdr_mode == "off"
    assert Entry.from_dict({"duration_min": 99999}).duration_min == core.MAX_DURATION_MIN
    assert Entry.from_dict({"freq_khz": None}).freq_khz == ""


def test_settings_from_dict_ignores_unknown_and_bad_types():
    s = Settings.from_dict({"port": "17362", "sweet_spot_base_hz": "abc", "nope": 1,
                            "scheduler_enabled": 0})
    assert s.port == 17362
    assert s.sweet_spot_base_hz == core.DEFAULT_SWEET_BASE
    assert s.scheduler_enabled is False
    assert Settings.from_dict(None) == Settings()


# ---------------------------------------------------------------- persistence

def test_document_round_trip_drops_blank_rows(tmp_path):
    st = Settings(port=17362, default_sdr_server="k:1", active_net={"label": "x"})
    doc = core.Document(st, [good_entry(), Entry(), good_entry(name="Two", record=True)])
    path = str(tmp_path / "sub" / "schedule.json")
    core.save_document(doc, path)
    assert not os.path.exists(path + ".tmp")
    raw = json.loads(open(path, encoding="utf-8").read())
    assert raw["schema_version"] == core.SCHEMA_VERSION
    assert len(raw["entries"]) == 2
    back = core.load_document(path)
    assert back.settings == st
    assert back.entries == [good_entry(), good_entry(name="Two", record=True)]


def test_document_rejects_non_object():
    with pytest.raises(ValueError):
        core.Document.from_json("[1, 2]")


def test_document_skips_non_dict_entries():
    doc = core.Document.from_json(json.dumps({"entries": [1, "x", {"name": "ok"}]}))
    assert [e.name for e in doc.entries] == ["ok"]


def test_default_config_path_shape():
    p = core.default_config_path()
    assert p.endswith(os.path.join(core.APP_ID, "schedule.json"))
    assert os.path.isabs(p)


# ---------------------------------------------------------------- time math

def test_next_occurrence_is_strictly_after_now():
    e = good_entry(day="Mon", start_utc="01:00")
    assert core.next_occurrence(e, at(0, 0, 59)) == at(0, 1, 0)
    assert core.next_occurrence(e, at(0, 1, 0)) == at(7, 1, 0)


@pytest.mark.parametrize("day,now,want", [
    ("Daily", at(0, 23, 0), at(1, 1, 0)),
    ("Weekdays", at(4, 2, 0), at(7, 1, 0)),       # Fri after the start -> next Mon
    ("Weekends", at(0, 0, 0), at(5, 1, 0)),       # Mon -> Sat
    ("Sun", at(0, 0, 0), at(6, 1, 0)),
])
def test_next_occurrence_days(day, now, want):
    assert core.next_occurrence(good_entry(day=day), now) == want


def test_next_occurrence_invalid():
    assert core.next_occurrence(good_entry(start_utc="nope"), at(0, 0, 0)) is None
    assert core.next_occurrence(good_entry(day="Someday"), at(0, 0, 0)) is None


def test_next_occurrence_converts_aware_local_time():
    plus2 = timezone(timedelta(hours=2))
    now = datetime(2024, 1, 1, 2, 30, tzinfo=plus2)   # = Mon 00:30 UTC
    assert core.next_occurrence(good_entry(), now) == at(0, 1, 0)


def test_local_start_text():
    assert core.local_start_text(good_entry(start_utc="bad"), at(0, 0, 0)) == ""
    assert core.local_start_text(good_entry(day="Daily"), at(0, 0, 0))
    txt = core.local_start_text(good_entry(day="Mon"), at(0, 0, 0))
    assert txt == at(0, 1, 0).astimezone().strftime("%a %H:%M")


def test_due_entries():
    st = Settings(default_sdr_server="")
    entries = [
        good_entry(name="a"),
        good_entry(name="off", enabled=False),
        good_entry(name="other time", start_utc="02:00"),
        good_entry(name="other day", day="Tue"),
        good_entry(name="daily", day="Daily", start_utc="1:00"),
        good_entry(name="bad default sdr", sdr_mode="default"),
    ]
    due = core.due_entries(entries, at(0, 1, 0, 30), st)
    assert [(i, e.name) for i, e in due] == [(0, "a"), (4, "daily")]


def test_minute_tracker_fires_current_minute_on_start():
    tr = core.MinuteTracker(at(0, 1, 0, 40))
    assert tr.advance(at(0, 1, 0, 41)) == ([at(0, 1, 0)], 0)
    assert tr.advance(at(0, 1, 0, 59)) == ([], 0)


def test_minute_tracker_catches_up_small_gaps():
    tr = core.MinuteTracker(at(0, 1, 0))
    tr.advance(at(0, 1, 0))
    mins, skipped = tr.advance(at(0, 1, 3, 5))
    assert mins == [at(0, 1, 1), at(0, 1, 2), at(0, 1, 3)] and skipped == 0


def test_minute_tracker_skips_long_gaps():
    tr = core.MinuteTracker(at(0, 1, 0))
    tr.advance(at(0, 1, 0))
    mins, skipped = tr.advance(at(0, 3, 0))
    assert mins == [at(0, 3, 0)] and skipped == 119


def test_minute_tracker_ignores_clock_going_back():
    tr = core.MinuteTracker(at(0, 1, 0))
    tr.advance(at(0, 1, 5))
    assert tr.advance(at(0, 1, 2)) == ([], 0)
    assert tr.advance(at(0, 1, 6)) == ([at(0, 1, 6)], 0)


# ---------------------------------------------------------------- tuning

def test_plan_tuning_documented_example():
    # CLAUDE.md: 7110 kHz, sweet spot 900, FLDigi 1500 -> dial 7109.400 kHz, carrier 1500
    dial, carrier, why = core.plan_tuning(7_110_000.0, 900, 1500)
    assert dial == 7_109_400.0 and carrier == 1500
    assert "7109.400" in why


def test_plan_tuning_same_sweet_spot_is_unshifted():
    assert core.plan_tuning(14_070_000.0, 1500, 1500)[:2] == (14_070_000.0, 1500)


def test_plan_tuning_higher_sweet_spot_raises_dial():
    assert core.plan_tuning(3_595_000.0, 2000, 1500)[:2] == (3_595_500.0, 1500)


def test_plan_tuning_blank_frequency():
    dial, carrier, _ = core.plan_tuning(None, 900, 1500)
    assert dial is None and carrier == 900


# ---------------------------------------------------------------- API helpers

@pytest.mark.parametrize("rig,want", [
    ("USB", "USB"), ("lsb", "LSB"), ("AM", "AM"), ("PKTUSB", "PKTUSB"),
    ("USB-D", "USB"), ("DATA_U", "USB"), ("DIGL", "LSB"), ("LSB-D", "LSB"),
    ("CW", None), ("FM", None), ("", None), (None, None),
])
def test_api_mode(rig, want):
    assert core.api_mode(rig) == want


@pytest.mark.parametrize("name,want", [
    ("Texas Digital Net", "Texas-Digital-Net_{date}T{time}"),
    ("  a   b ", "a-b_{date}T{time}"),
    ("Net/40m: #1", "Net_40m_-_1_{date}T{time}"),
    ("", "net_{date}T{time}"),
])
def test_recording_name(name, want):
    assert core.recording_name(name) == want


def test_api_error_text():
    assert core.api_error_text({"error": {"code": "busy", "message": "full"}}) == "busy: full"
    assert core.api_error_text({}) == "?:"
    assert core.api_error_text(None) == "?:"


def test_active_net_round_trip():
    a = core.ActiveNet("Net", at(0, 1, 0).isoformat(), at(0, 2, 0).isoformat(),
                       used_sdr=True, server="k:1", recording=True, recording_path="/x.wav")
    b = core.ActiveNet.from_dict(json.loads(json.dumps(a.to_dict())))
    assert b == a and b.end_dt() == at(0, 2, 0)


@pytest.mark.parametrize("d", [{}, {"label": "x", "end_utc": "not a date"},
                               {"label": "x", "started_utc": "", "end_utc": "", "bogus": 1}])
def test_active_net_from_bad_dict(d):
    assert core.ActiveNet.from_dict(d) is None
