#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 jeffreycentex
"""Minimal fake fldigi XML-RPC server for testing FLDigi Net Scheduler
without a radio. Mimics behaviour seen on fldigi 4.2.13:
  * modem.set_by_name to the current mode is a no-op
  * switching to another mode resets the carrier to the sweet spot (1500)
  * OFDM modes silently fail to switch
  * unknown methods return Fault -1
Usage: python3 mock_fldigi.py [port]      (default 7362)
"""
import sys
import threading
import time
from xmlrpc.server import SimpleXMLRPCServer, SimpleXMLRPCRequestHandler

sys.path.insert(0, __import__("os").path.dirname(__file__))
from sched_core import DEFAULT_DIGITAL_MODES  # noqa: E402

RIG_MODES = ["USB", "LSB", "AM", "FM", "CW", "PKTUSB"]
MODEMS = DEFAULT_DIGITAL_MODES + ["OFDM500F", "RSID_EOT", "NULL"]


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.modem, self.carrier = "BPSK31", 1500
        self.freq, self.rig_mode = 14070000.0, "USB"
        self.calls = []


S = State()


def rec(name, *a):
    S.calls.append((round(time.time(), 2), name, a))
    print(f"[mock] {name}{a}", flush=True)


def m_set_by_name(name):
    rec("modem.set_by_name", name)
    old = S.modem
    if name not in MODEMS:
        raise ValueError(f"unknown modem {name}")
    if name != S.modem and not name.startswith("OFDM"):
        def later():                       # fldigi switches asynchronously
            time.sleep(0.3)
            with S.lock:
                S.modem, S.carrier = name, 1500
        threading.Thread(target=later, daemon=True).start()
    return old


def m_set_carrier(c):
    rec("modem.set_carrier", c)
    old, S.carrier = S.carrier, int(c)
    return old


def m_set_freq(f):
    rec("main.set_frequency", f)
    old, S.freq = S.freq, float(f)
    return old


def m_rig_set_mode(m):
    rec("rig.set_mode", m)
    if m in RIG_MODES:
        S.rig_mode = m
    return None


class H(SimpleXMLRPCRequestHandler):
    def log_message(self, *a):
        pass


def make_server(port=7362):
    """Build the server (port 0 picks a free one). The handlers read the
    module-level S, so tests can reset it with `mock_fldigi.S = State()`."""
    srv = SimpleXMLRPCServer(("127.0.0.1", port), requestHandler=H, allow_none=True, logRequests=False)
    reg = srv.register_function
    reg(lambda: "4.2.13-mock", "fldigi.version")
    reg(lambda: "fldigi 4.2.13-mock", "fldigi.name_version")
    reg(lambda: S.modem, "modem.get_name")
    reg(lambda: MODEMS, "modem.get_names")
    reg(m_set_by_name, "modem.set_by_name")
    reg(lambda: S.carrier, "modem.get_carrier")
    reg(m_set_carrier, "modem.set_carrier")
    reg(lambda: S.freq, "main.get_frequency")
    reg(lambda: S.freq, "rig.get_frequency")
    reg(m_set_freq, "main.set_frequency")
    reg(lambda: S.rig_mode, "rig.get_mode")
    reg(lambda: RIG_MODES, "rig.get_modes")
    reg(m_rig_set_mode, "rig.set_mode")
    reg(lambda: "LSB" if S.rig_mode == "LSB" else "USB", "main.get_wf_sideband")
    return srv


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7362
    srv = make_server(port)
    print(f"mock fldigi listening on 127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
