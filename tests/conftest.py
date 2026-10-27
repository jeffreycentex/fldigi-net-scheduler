# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 jeffreycentex
"""Shared fixtures: the mock fldigi and mock Remote API servers run in-process
on free ports, so tests never touch a real fldigi, radio or SDR client."""
from __future__ import annotations

import os
import socket
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import mock_fldigi  # noqa: E402
import mock_kiwiremote  # noqa: E402


class Log:
    """Collects (level, message) pairs from sched_core's log callback."""
    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    def __call__(self, level: str, msg: str):
        self.lines.append((level, msg))

    def levels(self, level: str) -> list[str]:
        return [m for lv, m in self.lines if lv == level]

    def text(self) -> str:
        return "\n".join(m for _, m in self.lines)


@pytest.fixture
def log() -> Log:
    return Log()


def _serve(srv):
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


@pytest.fixture(scope="session")
def _fldigi_server():
    srv = mock_fldigi.make_server(0)
    srv.methods_seen = []
    dispatch = srv._dispatch

    def recording_dispatch(method, params):     # every XML-RPC method name, getters too
        srv.methods_seen.append(method)
        return dispatch(method, params)

    srv._dispatch = recording_dispatch
    _serve(srv)
    yield srv
    srv.shutdown()


@pytest.fixture
def fldigi(_fldigi_server):
    """Fresh mock fldigi state. Yields an object with host, port, state, methods."""
    mock_fldigi.S = mock_fldigi.State()
    _fldigi_server.methods_seen.clear()

    class F:
        host = "127.0.0.1"
        port = _fldigi_server.server_address[1]

        @property
        def state(self):
            return mock_fldigi.S

        @property
        def methods(self):
            return list(_fldigi_server.methods_seen)

    return F()


@pytest.fixture(scope="session")
def _kiwi_server():
    srv = _serve(ThreadingHTTPServer(("127.0.0.1", 0), mock_kiwiremote.H))
    yield srv
    srv.shutdown()


@pytest.fixture
def kiwi(_kiwi_server):
    """Fresh mock Remote API state. Yields an object with host, port, state,
    paths (POST paths in order) and body(path) (last body sent to a path)."""
    S = mock_kiwiremote.S
    S.clear()
    S.update({"state": "idle", "server": None, "password": False, "rec": None, "calls": []})

    class K:
        host = "127.0.0.1"
        port = _kiwi_server.server_address[1]
        state = S

        @property
        def paths(self):
            return [p.rsplit("/api/v1", 1)[-1] for p, _ in S["calls"]]

        def body(self, path):
            for p, b in reversed(S["calls"]):
                if p.endswith(path):
                    return b
            raise KeyError(path)

    return K()


@pytest.fixture
def closed_port() -> int:
    """A local port with nothing listening on it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
