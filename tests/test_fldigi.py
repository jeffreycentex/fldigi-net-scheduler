# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 jeffreycentex
"""fldigi XML-RPC actions against mock_fldigi, plus the never-transmit rule."""
from __future__ import annotations

import os
import re

import pytest

import sched_core as core
from sched_core import NO_CHANGE, Entry

from conftest import ROOT

# CLAUDE.md: the app must never call these.
TX_METHODS = ("main.tx", "main.tune", "main.run_macro", "text.add_tx",
              "wefax.send_file", "navtex.send_message")
# Everything the scheduler is allowed to change in fldigi.
ALLOWED_SETTERS = {"rig.set_mode", "main.set_frequency", "modem.set_by_name", "modem.set_carrier"}


def run(fldigi, log, e: Entry, base=1500) -> bool:
    return core.execute_entry(fldigi.host, fldigi.port, e, e.label(0), base,
                              switch_timeout=4.0, rpc_timeout=3.0, log=log)


def assert_never_transmitted(fldigi):
    for m in fldigi.methods:
        assert not m.startswith(TX_METHODS), f"transmit method called: {m}"
        assert "set" not in m or m in ALLOWED_SETTERS, f"unexpected setter called: {m}"


def test_execute_entry_full(fldigi, log):
    e = Entry(enabled=True, name="Net", freq_khz="7110", rig_mode="LSB",
              digital_mode="THOR22", sweet_spot_hz=900)
    assert run(fldigi, log, e) is True, log.text()
    s = fldigi.state
    assert s.rig_mode == "LSB"
    assert s.freq == 7_109_400.0
    assert s.modem == "THOR22"
    assert s.carrier == 1500
    assert not log.levels("error") and not log.levels("warn")
    assert_never_transmitted(fldigi)


def test_execute_entry_step_order(fldigi, log):
    """Rig mode goes first (radios move the VFO on a mode change), carrier last."""
    run(fldigi, log, Entry(freq_khz="14070", rig_mode="USB", digital_mode="MFSK16"))
    setters = [name for _, name, _ in fldigi.state.calls]
    assert setters == ["rig.set_mode", "main.set_frequency", "modem.set_by_name",
                       "modem.set_carrier"]


def test_same_modem_still_sets_carrier(fldigi, log):
    fldigi.state.carrier = 1234
    e = Entry(freq_khz="14070", rig_mode="USB", digital_mode="BPSK31", sweet_spot_hz=1000)
    assert run(fldigi, log, e)
    assert "modem.set_by_name" not in [n for _, n, _ in fldigi.state.calls]
    assert fldigi.state.carrier == 1500
    assert fldigi.state.freq == 14_069_500.0


def test_blank_frequency_leaves_dial_and_uses_row_sweet_spot(fldigi, log):
    e = Entry(freq_khz="", rig_mode=NO_CHANGE, digital_mode=NO_CHANGE, sweet_spot_hz=900)
    assert run(fldigi, log, e)
    assert fldigi.state.freq == 14_070_000.0
    assert fldigi.state.rig_mode == "USB"
    assert fldigi.state.carrier == 900
    assert [n for _, n, _ in fldigi.state.calls] == ["modem.set_carrier"]


def test_custom_fldigi_sweet_spot(fldigi, log):
    e = Entry(freq_khz="7110", rig_mode=NO_CHANGE, digital_mode=NO_CHANGE, sweet_spot_hz=900)
    assert run(fldigi, log, e, base=1000)
    assert fldigi.state.freq == 7_109_900.0 and fldigi.state.carrier == 1000


def test_modem_that_does_not_switch_is_reported(fldigi, log):
    e = Entry(freq_khz="7110", digital_mode="OFDM500F")
    assert run(fldigi, log, e) is False
    assert any("did not take" in m for m in log.levels("error"))
    assert fldigi.state.modem == "BPSK31"
    assert fldigi.state.carrier == 1500          # carrier step still runs


def test_unknown_rig_mode_warns_but_continues(fldigi, log):
    e = Entry(freq_khz="7110", rig_mode="WEIRD", digital_mode=NO_CHANGE)
    assert run(fldigi, log, e) is False
    assert any("not in fldigi's rig mode list" in m for m in log.levels("warn"))
    assert fldigi.state.freq == 7_110_000.0


def test_execute_entry_unreachable_does_not_raise(closed_port, log):
    e = Entry(freq_khz="7110", rig_mode="USB", digital_mode="MFSK16")
    ok = core.execute_entry("127.0.0.1", closed_port, e, "x", 1500, 1.0, 1.0, log)
    assert ok is False
    assert len(log.levels("error")) == 4         # each step reports its own failure
    assert "connection refused" in log.text() or "timed out" in log.text()   # Windows retries


def test_poll_status(fldigi):
    st = core.poll_status(fldigi.host, fldigi.port, 3.0)
    assert st == {"ok": True, "version": "4.2.13-mock", "modem": "BPSK31", "carrier": 1500,
                  "freq_hz": 14_070_000.0, "rig_mode": "USB"}


def test_poll_status_unreachable(closed_port):
    st = core.poll_status("127.0.0.1", closed_port, 1.0)
    assert st["ok"] is False and ("refused" in st["error"] or "timed out" in st["error"])


def test_fetch_lists_filters_unusable_modems(fldigi):
    rig, modems = core.fetch_lists(fldigi.host, fldigi.port, 3.0)
    assert "USB" in rig and "PKTUSB" in rig
    assert "THOR22" in modems and "NULL" in modems
    assert not [m for m in modems if m.startswith(("OFDM", "RSID_EOT"))]


def test_describe_error():
    import socket
    import xmlrpc.client
    assert core.describe_error(xmlrpc.client.Fault(-1, "nope")) == "XML-RPC fault -1: nope"
    assert core.describe_error(socket.timeout()) == "timed out"
    assert "resolve" in core.describe_error(socket.gaierror("x"))
    assert core.describe_error(ValueError("v")) == "ValueError: v"


def test_default_modes_exclude_unusable():
    assert not [m for m in core.DEFAULT_DIGITAL_MODES if core.EXCLUDE_DIGITAL.search(m)]
    assert len(set(core.DEFAULT_DIGITAL_MODES)) == len(core.DEFAULT_DIGITAL_MODES)


@pytest.mark.parametrize("fn", ["sched_core.py", "fldigi_net_scheduler.py", "nets_csv.py"])
def test_source_never_calls_transmit_methods(fn):
    """Static guard for the never-transmit rule in CLAUDE.md."""
    src = open(os.path.join(ROOT, fn), encoding="utf-8").read()
    pattern = r"\.(" + "|".join(re.escape(m) for m in TX_METHODS) + r")"
    pattern += r"|[\"'](" + "|".join(re.escape(m) for m in TX_METHODS) + r")"
    assert not re.search(pattern, src), f"{fn} references a transmit method"
