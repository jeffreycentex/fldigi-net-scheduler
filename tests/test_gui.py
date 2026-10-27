# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 jeffreycentex
"""Headless (offscreen) GUI tests. The window always points at the mock
servers, so a real fldigi or Remote on this computer is never touched."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import fldigi_net_scheduler as gui  # noqa: E402
import sched_core as core  # noqa: E402
from sched_core import Entry, Settings  # noqa: E402

MON_0100 = datetime(2024, 1, 1, 1, 0, 20, tzinfo=timezone.utc)   # a Monday


@pytest.fixture(scope="session")
def app():
    a = QApplication.instance() or QApplication(["test"])
    a.setApplicationName(gui.APP_ID)
    return a


def wait_until(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


NETS = [
    Entry(enabled=True, name="Texas Digital Net", day="Mon", start_utc="01:00", freq_khz="7110",
          rig_mode="LSB", digital_mode="THOR22", sweet_spot_hz=900, sdr_mode="default",
          record=True, duration_min=30),
    Entry(enabled=False, name="Second", day="Daily", start_utc="02:30", freq_khz="14070",
          rig_mode="USB", digital_mode="BPSK31"),
    Entry(enabled=True, name="Third", day="Weekends", start_utc="13:00", sdr_mode="custom",
          sdr_server="c.example:8073", sdr_password="pw", duration_min=15),
]


@pytest.fixture
def config(tmp_path, fldigi, kiwi):
    path = str(tmp_path / "schedule.json")
    st = Settings(host=fldigi.host, port=fldigi.port, api_host=kiwi.host, api_port=kiwi.port,
                  scheduler_enabled=False, default_sdr_server="kiwi.example:8073")
    core.save_document(core.Document(st, list(NETS)), path)
    return path


@pytest.fixture
def window(app, config):
    windows = []

    def make(path=config):
        w = gui.MainWindow(path)
        windows.append(w)
        return w

    yield make
    for w in windows:
        w.close()
        w.deleteLater()
    QApplication.processEvents()


def test_qt_and_core_config_paths_match(app):
    """CLAUDE.md: sched_core.default_config_path mirrors Qt's AppConfigLocation."""
    norm = lambda p: os.path.normcase(os.path.normpath(p))
    assert norm(gui.default_config_path()) == norm(core.default_config_path())


def test_loads_into_grid_and_saves_back(window, config):
    w = window()
    assert w.entries()[:3] == NETS
    assert w.table.rowCount() >= core.MIN_ROWS
    w._add_row()
    w._save_now()
    doc = core.load_document(config)
    assert doc.entries == NETS                    # blank rows are not saved
    assert doc.settings.default_sdr_server == "kiwi.example:8073"


def test_empty_file_gets_min_rows(window, tmp_path):
    w = window(str(tmp_path / "new.json"))
    assert w.table.rowCount() == core.MIN_ROWS
    assert all(e.is_blank() and not e.enabled for e in w.entries())


def test_corrupt_file_is_renamed_not_overwritten(window, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    w = window(str(path))
    bad = [p for p in os.listdir(tmp_path) if p.startswith("s.json.bad-")]
    assert len(bad) == 1
    assert (tmp_path / bad[0]).read_text(encoding="utf-8") == "{not json"
    assert all(e.is_blank() for e in w.entries())


def test_connects_and_fetches_lists(window, fldigi):
    w = window()
    wait_until(lambda: w._connected is True)
    wait_until(lambda: "SCAMPFSK" in w.digital_modes and "OFDM500F" not in w.digital_modes)
    assert "rig.get_modes" in fldigi.methods


def test_manual_run_and_stop(window, fldigi, kiwi, config):
    w = window()
    w._execute(0, w.entries()[0])
    wait_until(lambda: not w._starting)
    a = w.active
    assert a and a.label == "Texas Digital Net" and a.used_sdr and a.recording
    assert fldigi.state.freq == 7_109_400.0 and fldigi.state.modem == "THOR22"
    assert kiwi.state["state"] == "connected" and kiwi.state["rec"]

    w._save_now()                                  # the active net survives restarts
    assert json.load(open(config, encoding="utf-8"))["settings"]["active_net"]["label"] == a.label
    w2 = window()
    assert w2.active == a

    w._stop_active()
    wait_until(lambda: kiwi.state["rec"] is None and kiwi.state["state"] == "idle")
    assert w.active is None


def test_active_net_ends_when_duration_runs_out(window, kiwi):
    w = window()
    w._execute(0, w.entries()[0])
    wait_until(lambda: not w._starting and w.active is not None)
    w.active.end_utc = (core.utcnow() - timedelta(seconds=1)).isoformat()
    w._on_clock()
    assert w.active is None
    wait_until(lambda: kiwi.paths[-2:] == ["/record/stop", "/disconnect"])


def test_scheduler_fires_due_entry(window, fldigi, kiwi, monkeypatch):
    w = window()
    w.sched_chk.setChecked(True)
    monkeypatch.setattr(core, "utcnow", lambda: MON_0100)
    w.tracker = core.MinuteTracker(MON_0100)
    w._on_minute()
    assert w._starting
    wait_until(lambda: not w._starting)
    assert w.active and w.active.label == "Texas Digital Net"
    assert fldigi.state.rig_mode == "LSB"
    w._on_minute()                                 # same minute: not run twice
    assert not w._starting


def test_paused_scheduler_runs_nothing(window, fldigi, monkeypatch):
    w = window()
    assert not w.sched_chk.isChecked()
    monkeypatch.setattr(core, "utcnow", lambda: MON_0100)
    w.tracker = core.MinuteTracker(MON_0100)
    w._on_minute()
    assert not w._starting and w.active is None
