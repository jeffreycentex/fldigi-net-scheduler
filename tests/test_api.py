# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 jeffreycentex
"""KiwiSDR Remote / ICOM Remote API client and the net start/end sequence,
against mock_kiwiremote."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import sched_core as core
from sched_core import ActiveNet, Entry, Settings

NOW = datetime(2024, 1, 1, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
def api(kiwi):
    return core.KiwiApi(kiwi.host, kiwi.port)


@pytest.fixture
def settings(kiwi):
    return Settings(api_host=kiwi.host, api_port=kiwi.port,
                    default_sdr_server="kiwi.example:8073", default_sdr_password="secret")


def net(**kw) -> Entry:
    d = dict(enabled=True, name="Texas Digital Net", day="Mon", start_utc="01:00",
             freq_khz="7110", rig_mode="USB", digital_mode="THOR22", sweet_spot_hz=900,
             sdr_mode="default", record=True, duration_min=60)
    d.update(kw)
    return Entry(**d)


def start(api, e, settings, log, kiwi, previous=None):
    """start_net with a fake fldigi step that leaves a marker in the call list."""
    def fldigi():
        kiwi.state["calls"].append(("/api/v1/<fldigi>", {}))
        return True
    return core.start_net(api, e, e.label(0), settings, fldigi, log, previous, now=NOW)


def test_start_net_full_sequence(api, settings, log, kiwi):
    a = start(api, net(), settings, log, kiwi)
    assert kiwi.paths == ["/connect", "/tune", "/<fldigi>", "/record/start"]

    c = kiwi.body("/connect")
    assert c["server"] == "kiwi.example:8073" and c["password"] == "***"
    assert c["wait"] == core.CONNECT_WAIT_S and c["session_limit_min"] == 62

    assert kiwi.body("/tune") == {"freq_hz": 7_109_400, "mode": "USB"}

    r = kiwi.body("/record/start")
    assert r == {"name": "Texas-Digital-Net_{date}T{time}", "max_seconds": 60 * 60 + 120}

    assert a == ActiveNet(label="Texas Digital Net", started_utc=NOW.isoformat(),
                          end_utc=(NOW + timedelta(minutes=60)).isoformat(), used_sdr=True,
                          server="kiwi.example:8073", recording=True,
                          recording_path=a.recording_path)
    assert a.recording_path.startswith("/tmp/kiwi/Texas-Digital-Net_")
    assert not log.levels("error")
    assert "secret" not in log.text()


def test_tune_maps_rig_mode_and_never_sends_digital_mode(api, settings, log, kiwi):
    start(api, net(rig_mode="USB-D", sdr_mode="off", record=False), settings, log, kiwi)
    assert kiwi.body("/tune") == {"freq_hz": 7_109_400, "mode": "USB"}


def test_tune_unmapped_mode_sends_frequency_only(api, settings, log, kiwi):
    start(api, net(rig_mode="CW", sdr_mode="off", record=False), settings, log, kiwi)
    assert kiwi.body("/tune") == {"freq_hz": 7_109_400}
    assert any("no API equivalent" in m for m in log.levels("warn"))


def test_tune_skipped_when_nothing_to_send(api, settings, log, kiwi):
    e = net(freq_khz="", rig_mode=core.NO_CHANGE, sdr_mode="off", record=False)
    assert start(api, e, settings, log, kiwi) is None
    assert kiwi.paths == ["/<fldigi>"]


def test_record_only_net(api, settings, log, kiwi):
    """ICOM Remote nets: SDR Off, record only."""
    a = start(api, net(sdr_mode="off"), settings, log, kiwi)
    assert kiwi.paths == ["/tune", "/<fldigi>", "/record/start"]
    assert a.recording and not a.used_sdr and a.server == ""


def test_custom_sdr(api, settings, log, kiwi):
    e = net(sdr_mode="custom", sdr_server="custom.example:8073", sdr_password="", record=False)
    a = start(api, e, settings, log, kiwi)
    assert kiwi.body("/connect")["server"] == "custom.example:8073"
    assert kiwi.body("/connect")["password"] == ""
    assert a.used_sdr and a.server == "custom.example:8073" and not a.recording


def test_nothing_to_end_returns_none(api, settings, log, kiwi):
    assert start(api, net(sdr_mode="off", record=False), settings, log, kiwi) is None


def test_no_duration_returns_none_with_warning(api, settings, log, kiwi):
    assert start(api, net(duration_min=0), settings, log, kiwi) is None
    assert "session_limit_min" not in kiwi.body("/connect")
    assert any("no duration" in m for m in log.levels("warn"))


def test_default_sdr_without_server(api, log, kiwi):
    st = Settings(api_host=kiwi.host, api_port=kiwi.port)
    a = start(api, net(), st, log, kiwi)
    assert "/connect" not in kiwi.paths and "/record/start" not in kiwi.paths
    assert a is None
    assert any("no server configured" in m for m in log.levels("error"))


def test_connect_502_skips_recording(api, settings, log, kiwi):
    a = start(api, net(sdr_mode="custom", sdr_server="fail.example"), settings, log, kiwi)
    assert "/record/start" not in kiwi.paths
    assert "/<fldigi>" in kiwi.paths               # fldigi is still tuned
    assert a.used_sdr and not a.recording          # the link is still dropped at the end
    assert any("recording not started" in m for m in log.levels("error"))


@pytest.mark.parametrize("server", ["busy.example", "slow.example"])
def test_connect_503_504_carry_on(api, settings, log, kiwi, server):
    a = start(api, net(sdr_mode="custom", sdr_server=server), settings, log, kiwi)
    assert kiwi.paths[-1] == "/record/start"
    assert a.used_sdr and a.recording
    assert any("keeps retrying" in m for m in log.levels("warn"))


def test_record_409_stops_and_retries_once(api, settings, log, kiwi):
    api.record_start("someone_else", 60)
    kiwi.state["calls"].clear()
    a = start(api, net(sdr_mode="off"), settings, log, kiwi)
    assert kiwi.paths == ["/tune", "/<fldigi>", "/record/start", "/record/stop", "/record/start"]
    assert a.recording and "Texas-Digital-Net" in a.recording_path


def test_previous_net_ended_first_keeps_sdr_link_when_next_connects(api, settings, log, kiwi):
    prev = start(api, net(name="First"), settings, log, kiwi)
    kiwi.state["calls"].clear()
    start(api, net(name="Second"), settings, log, kiwi, previous=prev)
    assert kiwi.paths == ["/record/stop", "/connect", "/tune", "/<fldigi>", "/record/start"]
    assert prev.used_sdr                          # caller's object is not modified


def test_previous_net_disconnected_when_next_has_no_sdr(api, settings, log, kiwi):
    prev = start(api, net(name="First"), settings, log, kiwi)
    kiwi.state["calls"].clear()
    start(api, net(name="Second", sdr_mode="off"), settings, log, kiwi, previous=prev)
    assert kiwi.paths[:2] == ["/record/stop", "/disconnect"]


def test_end_net(api, settings, log, kiwi):
    a = start(api, net(), settings, log, kiwi)
    kiwi.state["calls"].clear()
    core.end_net(api, a, log)
    assert kiwi.paths == ["/record/stop", "/disconnect"]
    assert kiwi.state["state"] == "idle" and kiwi.state["rec"] is None
    assert any("recording stopped" in m for m in log.levels("ok"))


def test_end_net_record_only_does_not_disconnect(api, settings, log, kiwi):
    a = start(api, net(sdr_mode="off"), settings, log, kiwi)
    kiwi.state["calls"].clear()
    core.end_net(api, a, log)
    assert kiwi.paths == ["/record/stop"]


def test_end_net_already_stopped_warns(api, log, kiwi):
    core.end_net(api, ActiveNet("x", "", NOW.isoformat(), recording=True), log)
    assert any("already stopped" in m for m in log.levels("warn"))


def test_api_unreachable(closed_port, settings, log, kiwi):
    api = core.KiwiApi("127.0.0.1", closed_port, timeout=1.0)
    called = []
    a = core.start_net(api, net(), "x", settings, lambda: called.append(1) or True, log, now=NOW)
    assert called == [1]                          # fldigi is tuned regardless
    assert a is None
    assert any("not running" in m for m in log.levels("info"))
    core.end_net(api, ActiveNet("x", "", NOW.isoformat(), used_sdr=True, recording=True), log)
    assert any("not reachable" in m for m in log.levels("error"))


def test_api_status_summary(api, settings, log, kiwi, closed_port):
    s = core.api_status_summary(api)
    assert s["ok"] and s["state"] == "idle" and not s["recording"]
    start(api, net(), settings, log, kiwi)
    s = core.api_status_summary(api)
    assert s["state"] == "connected" and s["server"] == "kiwi.example:8073" and s["recording"]
    s = core.api_status_summary(core.KiwiApi("127.0.0.1", closed_port, timeout=1.0))
    assert s["ok"] is False


def test_http_error_body_is_returned(api):
    code, r = api._call("POST", "/nope")
    assert code == 404 and r["error"]["code"] == "not_found"


def test_headers(monkeypatch):
    seen = {}

    class Resp:
        status = 200

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    api = core.KiwiApi(token=" tok ")

    def fake_open(req, timeout):
        seen.update(req.header_items())
        seen["timeout"] = timeout
        return Resp()

    monkeypatch.setattr(api._opener, "open", fake_open)
    assert api.record_stop() == (200, {"ok": True})
    assert seen["Content-type"] == "application/json"
    assert seen["Authorization"] == "Bearer tok"
    seen.clear()
    api.status()
    assert "Content-type" not in seen
    api.connect("k", wait=15)
    assert seen["timeout"] == 25
