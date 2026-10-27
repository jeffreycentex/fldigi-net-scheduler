#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 jeffreycentex
"""Minimal fake KiwiSDR Remote / ICOM Remote API (v1) for testing FLDigi Net
Scheduler without the real client. Implements /status, /connect,
/disconnect, /tune, /record/start and /record/stop with the documented status
codes. Special servers for testing:
  busy.example     -> 503 busy
  fail.example     -> 502 connect_failed
  slow.example     -> 504 timeout
Usage: python3 mock_kiwiremote.py [port]   (default 7373)
"""
import json
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
S = {"state": "idle", "server": None, "password": False,
     "rec": None, "calls": []}


def now_fields():
    t = datetime.now(timezone.utc)
    return t.strftime("%Y%m%d"), t.strftime("%H%M%SZ")


def rec_info(active):
    r = S["rec"]
    if not r:
        return None
    return {"active": active, "path": r["path"], "seconds": round(time.time() - r["t0"], 1),
            "gap_seconds": 0.0, "max_seconds": r["max"]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _err(self, code, ecode, msg):
        self._send(code, {"ok": False, "error": {"code": ecode, "message": msg}})

    def do_GET(self):
        if self.path.rstrip("/") == "/api/v1/status":
            with LOCK:
                self._send(200, {"ok": True, "app_version": "mock", "state": S["state"],
                                 "connected": S["state"] == "connected", "server": S["server"],
                                 "recording": rec_info(True) or {"active": False}})
        else:
            self._err(404, "not_found", self.path)

    def do_POST(self):
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._err(415, "unsupported_media_type", "need application/json")
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._err(400, "bad_json", "bad json")
        path = self.path.rstrip("/")
        safe = {k: ("***" if k == "password" and v else v) for k, v in body.items()}
        S["calls"].append((path, safe))
        print(f"[kiwi-mock] POST {path} {safe}", flush=True)
        with LOCK:
            if path == "/api/v1/connect":
                srv = body.get("server")
                if not srv:
                    return self._err(400, "bad_request", "server required")
                S["server"], S["password"] = srv, bool(body.get("password"))
                if srv.startswith("busy."):
                    S["state"] = "busy"
                    return self._err(503, "busy", "all channels in use")
                if srv.startswith("fail."):
                    S["state"] = "failed"
                    return self._err(502, "connect_failed", "bad password")
                if srv.startswith("slow."):
                    S["state"] = "connecting"
                    return self._err(504, "timeout", "no answer yet")
                S["state"] = "connected"
                return self._send(200, {"ok": True, "state": "connected", "server": srv})
            if path == "/api/v1/disconnect":
                stopped = rec_info(False)
                S["rec"], S["state"], S["server"] = None, "idle", None
                return self._send(200, {"ok": True, "recording_stopped": stopped})
            if path == "/api/v1/tune":
                tune = S.setdefault("tune", {"freq_hz": 0, "mode": "USB", "passband": 0})
                if "freq_khz" in body:
                    tune["freq_hz"] = int(float(body["freq_khz"]) * 1000)
                if "freq_hz" in body:
                    tune["freq_hz"] = int(body["freq_hz"])
                if "mode" in body:
                    if body["mode"] not in ("USB", "LSB", "AM", "PKTUSB", "PKTLSB", "SAM"):
                        return self._err(400, "bad_request", f"bad mode {body['mode']}")
                    tune["mode"] = {"PKTUSB": "USB", "PKTLSB": "LSB", "SAM": "AM"}.get(body["mode"], body["mode"])
                return self._send(200, {"ok": True, "tune": tune})
            if path == "/api/v1/record/start":
                if S["rec"]:
                    return self._err(409, "conflict", "already recording")
                d, t = now_fields()
                name = (body.get("name") or "kiwi_{date}T{time}").replace("{date}", d).replace("{time}", t)
                name = re.sub(r"[^A-Za-z0-9._+-]", "_", name)
                S["rec"] = {"path": f"/tmp/kiwi/{name}.wav", "t0": time.time(),
                            "max": body.get("max_seconds")}
                return self._send(200, {"ok": True, "recording": rec_info(True)})
            if path == "/api/v1/record/stop":
                was = S["rec"] is not None
                info = rec_info(False) or {"active": False}
                S["rec"] = None
                return self._send(200, {"ok": True, "was_recording": was,
                                        "recording": dict(info, reason="api")})
        self._err(404, "not_found", path)


def serve(port=7373):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"mock KiwiSDR Remote / ICOM Remote API on http://127.0.0.1:{port}/api/v1", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 7373)
