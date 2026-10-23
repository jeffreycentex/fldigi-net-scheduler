# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 jeffreycentex
"""Qt-free core for FLDigi Net Scheduler: schedule model, time math, JSON
persistence and the fldigi XML-RPC actions. Imported by fldigi_net_scheduler.py.
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
import xmlrpc.client
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

SCHEMA_VERSION = 2
MIN_ROWS = 30
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7362
DEFAULT_SWEET_BASE = 1500

DAYS = ["Daily", "Weekdays", "Weekends", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
NO_CHANGE = "(no change)"

# Fallback lists, used until fldigi reports its own (rig.get_modes / modem.get_names).
DEFAULT_RIG_MODES = ["USB", "LSB", "AM", "FM", "CW", "CW-R", "RTTY", "RTTY-R",
                     "PKTUSB", "PKTLSB", "USB-D", "LSB-D", "DATA-U", "DATA-L"]

# From modem.get_names on fldigi 4.2.13, minus modes that don't switch cleanly.
DEFAULT_DIGITAL_MODES = """
CW RTTY SSB
BPSK31 BPSK63 BPSK63F BPSK125 BPSK250 BPSK500 BPSK1000
QPSK31 QPSK63 QPSK125 QPSK250 QPSK500
PSK125R PSK250R PSK500R PSK1000R
PSK125C12 PSK250C6 PSK500C2 PSK500C4 PSK800C2 PSK1000C2
PSK63RC4 PSK63RC5 PSK63RC10 PSK63RC20 PSK63RC32
PSK125RC4 PSK125RC5 PSK125RC10 PSK125RC12 PSK125RC16
PSK250RC2 PSK250RC3 PSK250RC5 PSK250RC6 PSK250RC7
PSK500RC2 PSK500RC3 PSK500RC4 PSK800RC2 PSK1000RC2
8PSK125 8PSK125FL 8PSK125F 8PSK250 8PSK250FL 8PSK250F
8PSK500 8PSK500F 8PSK1000 8PSK1000F 8PSK1200F
CONTESTIA Cont-4/125 Cont-4/250 Cont-4/500 Cont-4/1K Cont-4/2K
Cont-8/125 Cont-8/250 Cont-8/500 Cont-8/1K Cont-8/2K
Cont-16/250 Cont-16/500 Cont-16/1K Cont-16/2K Cont-32/1K Cont-32/2K
Cont-64/500 Cont-64/1K Cont-64/2K
OLIVIA OLIVIA-4/125 OLIVIA-4/250 OLIVIA-4/500 OLIVIA-4/1K OLIVIA-4/2K
OLIVIA-8/125 OLIVIA-8/250 OLIVIA-8/500 OLIVIA-8/1K OLIVIA-8/2K
OLIVIA-16/500 OLIVIA-16/1K OLIVIA-16/2K OLIVIA-32/1K OLIVIA-32/2K
OLIVIA-64/500 OLIVIA-64/1K OLIVIA-64/2K
THORM THOR4 THOR5 THOR8 THOR11 THOR16 THOR22 THOR25 THOR32 THOR44 THOR56
THOR25x4 THOR50x1 THOR50x2 THOR100
MFSK4 MFSK8 MFSK11 MFSK16 MFSK22 MFSK31 MFSK32 MFSK64 MFSK128 MFSK64L MFSK128L
DOMEXM DOMEX4 DOMEX5 DOMEX8 DOMX11 DOMX16 DOMX22 DOMX44 DOMX88
MT63-500S MT63-500L MT63-1KS MT63-1KL MT63-2KS MT63-2KL
THROB1 THROB2 THROB4 THRBX1 THRBX2 THRBX4
FELDHELL SLOWHELL HELLX5 HELLX9 HELL80 FSKH245 FSKH105
SCAMPFSK SCAMPOOK SCFSKFST SCFSKSLW SCOOKSLW SCFSKVSL
""".split()

# Names fldigi lists but that are not usable modems (OFDM failed to switch on 4.2.13).
EXCLUDE_DIGITAL = re.compile(r"^(RSID_EOT|OFDM)", re.I)

SWEET_MIN, SWEET_MAX = 100, 3500     # audio Hz accepted for a net's sweet spot


# ---------------------------------------------------------------- validation

def normalize_time(s: str) -> Optional[str]:
    """Accept 'HH:MM', 'H:MM' or 'HHMM' (24 h). Return 'HH:MM' or None."""
    m = re.fullmatch(r"\s*(\d{1,2}):?(\d{2})\s*", s or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def parse_freq_khz(s: str) -> Optional[float]:
    """kHz string -> float kHz, or None if blank/invalid."""
    s = (s or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if 0 < v < 10_000_000 else None



def day_matches(day: str, weekday: int) -> bool:
    """weekday: 0=Mon .. 6=Sun (UTC)."""
    if day == "Daily":
        return True
    if day == "Weekdays":
        return weekday < 5
    if day == "Weekends":
        return weekday >= 5
    try:
        return DAYS.index(day) - 3 == weekday
    except ValueError:
        return False


# ---------------------------------------------------------------- model

@dataclass
class Entry:
    enabled: bool = True
    name: str = ""
    day: str = "Mon"
    start_utc: str = ""
    freq_khz: str = ""
    rig_mode: str = "USB"
    digital_mode: str = NO_CHANGE
    sweet_spot_hz: int = DEFAULT_SWEET_BASE   # net's audio offset above/below the dial

    def is_blank(self) -> bool:
        return not (self.name.strip() or self.start_utc.strip() or self.freq_khz.strip())

    def problems(self) -> list[str]:
        p = []
        if normalize_time(self.start_utc) is None:
            p.append("start time must be HH:MM (24 h UTC)")
        if self.freq_khz.strip() and parse_freq_khz(self.freq_khz) is None:
            p.append("frequency must be a number in kHz")
        if self.day not in DAYS:
            p.append(f"unknown day '{self.day}'")
        if not SWEET_MIN <= int(self.sweet_spot_hz) <= SWEET_MAX:
            p.append(f"sweet spot must be {SWEET_MIN}-{SWEET_MAX} Hz")
        return p

    def is_schedulable(self) -> bool:
        return self.enabled and not self.is_blank() and not self.problems()

    def label(self, row: int) -> str:
        return self.name.strip() or f"row {row + 1}"

    @classmethod
    def from_dict(cls, d: dict) -> "Entry":
        e = cls()
        for k in asdict(e):
            if k in d:
                setattr(e, k, d[k])
        e.enabled = bool(e.enabled)
        e.name, e.day, e.start_utc = str(e.name), str(e.day), str(e.start_utc)
        e.freq_khz = "" if e.freq_khz in (None, "") else str(e.freq_khz)
        e.rig_mode, e.digital_mode = str(e.rig_mode), str(e.digital_mode)
        try:
            if "sweet_spot_hz" not in d and "sweet_spot_offset_hz" in d:
                # schema 1 stored an offset from 1500
                e.sweet_spot_hz = DEFAULT_SWEET_BASE + int(d["sweet_spot_offset_hz"])
            e.sweet_spot_hz = int(e.sweet_spot_hz)
        except (TypeError, ValueError):
            e.sweet_spot_hz = DEFAULT_SWEET_BASE
        return e


@dataclass
class Settings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    sweet_spot_base_hz: int = DEFAULT_SWEET_BASE
    scheduler_enabled: bool = True
    poll_interval_s: int = 5
    mode_switch_timeout_s: float = 4.0
    xmlrpc_timeout_s: float = 3.0
    window_geometry: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        s = cls()
        defaults = asdict(s)
        for k, v in (d or {}).items():
            if k in defaults:
                try:
                    setattr(s, k, type(defaults[k])(v))
                except (TypeError, ValueError):
                    pass
        return s


@dataclass
class Document:
    settings: Settings = field(default_factory=Settings)
    entries: list[Entry] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({
            "app": "FLDigi Net Scheduler",
            "schema_version": SCHEMA_VERSION,
            "settings": asdict(self.settings),
            "entries": [asdict(e) for e in self.entries if not e.is_blank()],
        }, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Document":
        d = json.loads(text)
        if not isinstance(d, dict):
            raise ValueError("top level is not a JSON object")
        return cls(Settings.from_dict(d.get("settings", {})),
                   [Entry.from_dict(x) for x in d.get("entries", []) if isinstance(x, dict)])


def save_document(doc: Document, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(doc.to_json())
    os.replace(tmp, path)   # atomic on all three platforms


def load_document(path: str) -> Document:
    with open(path, encoding="utf-8") as fh:
        return Document.from_json(fh.read())


# ---------------------------------------------------------------- time math

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def floor_minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def next_occurrence(e: Entry, now: datetime) -> Optional[datetime]:
    """Next UTC datetime strictly after `now` at which entry e starts."""
    t = normalize_time(e.start_utc)
    if t is None or e.day not in DAYS:
        return None
    h, m = map(int, t.split(":"))
    base = now.astimezone(timezone.utc)
    for d in range(8):
        day = (base + timedelta(days=d)).date()
        cand = datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)
        if cand > base and day_matches(e.day, cand.weekday()):
            return cand
    return None


def local_start_text(e: Entry, now: datetime) -> str:
    """Human text for the entry's start in the computer's local time zone."""
    occ = next_occurrence(e, now)
    if occ is None:
        return ""
    loc = occ.astimezone()
    if e.day in ("Daily", "Weekdays", "Weekends"):
        shift = (loc.date() - occ.date()).days
        txt = loc.strftime("%H:%M")
        if shift:
            txt += f" ({shift:+d} day)"
        return txt
    return loc.strftime("%a %H:%M")


def due_entries(entries: list[Entry], minute: datetime) -> list[tuple[int, Entry]]:
    """Entries whose start matches this UTC minute."""
    minute = minute.astimezone(timezone.utc)
    hhmm = minute.strftime("%H:%M")
    return [(i, e) for i, e in enumerate(entries)
            if e.is_schedulable() and normalize_time(e.start_utc) == hhmm
            and day_matches(e.day, minute.weekday())]


class MinuteTracker:
    """Yields each UTC minute exactly once, catching up after a late timer
    but not replaying a long gap (sleep/hibernate, clock jumps)."""
    MAX_CATCHUP = 5

    def __init__(self, now: datetime):
        # Start one minute back so an entry for the current minute still fires
        # if the app is started during that minute.
        self.last = floor_minute(now) - timedelta(minutes=1)

    def advance(self, now: datetime) -> tuple[list[datetime], int]:
        cur = floor_minute(now)
        if cur <= self.last:
            return [], 0           # clock went backwards or same minute
        gap = int((cur - self.last).total_seconds() // 60)
        if gap > self.MAX_CATCHUP:
            self.last = cur
            return [cur], gap - 1  # run only the current minute, report skipped
        mins = [self.last + timedelta(minutes=i) for i in range(1, gap + 1)]
        self.last = cur
        return mins, 0


# ---------------------------------------------------------------- fldigi XML-RPC

class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout: float):
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def make_proxy(host: str, port: int, timeout: float = 3.0):
    return xmlrpc.client.ServerProxy(f"http://{host}:{port}/",
                                     transport=_TimeoutTransport(timeout), allow_none=True)


def describe_error(e: BaseException) -> str:
    if isinstance(e, xmlrpc.client.Fault):
        return f"XML-RPC fault {e.faultCode}: {e.faultString}"
    if isinstance(e, ConnectionRefusedError):
        return "connection refused (is fldigi running with XML-RPC on that port?)"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "timed out"
    if isinstance(e, socket.gaierror):
        return f"cannot resolve host ({e})"
    return f"{type(e).__name__}: {e}"


Log = Callable[[str, str], None]     # (level, message); level in info/ok/warn/error


def get_dial_hz(p) -> float:
    """rig.get_frequency on current fldigi (main.get_frequency is deprecated)."""
    try:
        return float(p.rig.get_frequency())
    except xmlrpc.client.Fault:
        return float(p.main.get_frequency())


def poll_status(host: str, port: int, timeout: float) -> dict:
    try:
        p = make_proxy(host, port, timeout)
        st = {"ok": True,
              "version": p.fldigi.version(),
              "modem": p.modem.get_name(),
              "carrier": p.modem.get_carrier(),
              "freq_hz": get_dial_hz(p)}
        try:
            st["rig_mode"] = p.rig.get_mode()
        except Exception:
            st["rig_mode"] = ""
        return st
    except Exception as e:
        return {"ok": False, "error": describe_error(e)}


def fetch_lists(host: str, port: int, timeout: float) -> tuple[list[str], list[str]]:
    p = make_proxy(host, port, timeout)
    names = [n for n in p.modem.get_names() if not EXCLUDE_DIGITAL.search(n)]
    try:
        rig_modes = list(p.rig.get_modes())
    except Exception:
        rig_modes = []
    return rig_modes, names


_NO_OFFSET_MODES = {"AM", "FM", "NFM", "WFM", "FMN", "AM-D", "FM-D", "CW", "CW-R", "CWR",
                    "RTTY", "RTTY-R", "RTTYR", "FSK", "FSK-R"}


def rig_sideband(mode: str) -> Optional[str]:
    """'USB', 'LSB', 'NONE' (AM/FM/CW/FSK: no audio-offset shift) or None (unknown)."""
    m = (mode or "").upper().replace("_", "-").strip()
    if not m or m == NO_CHANGE.upper():
        return None
    if m in _NO_OFFSET_MODES:
        return "NONE"
    if "LSB" in m or m in ("DIGL", "DATA-L", "PKT-L") or m.endswith("-L"):
        return "LSB"
    if "USB" in m or m in ("DIGU", "DATA-U", "PKT-U", "DATA") or m.endswith("-U"):
        return "USB"
    return None


def plan_tuning(freq_hz: Optional[float], sweet_hz: int, centre_hz: int,
                sideband: Optional[str]) -> tuple[Optional[float], int, str]:
    """Where to put the dial and the fldigi carrier so the net's signal
    (published dial + sweet spot audio) lands at our centre (1500 Hz).

    USB: RF = dial + audio  ->  dial = freq + (sweet - centre)
    LSB: RF = dial - audio  ->  dial = freq - (sweet - centre)
    Returns (dial_hz or None, carrier_hz, explanation)."""
    shift = int(sweet_hz) - int(centre_hz)
    if freq_hz is None:
        return None, int(sweet_hz), "no frequency given: dial unchanged, carrier on the sweet spot"
    if sideband == "USB":
        return freq_hz + shift, int(centre_hz), (
            f"signal at {(freq_hz + sweet_hz) / 1000:.3f} kHz RF (USB); dial "
            f"{shift:+d} Hz so it sits at {centre_hz} Hz audio")
    if sideband == "LSB":
        return freq_hz - shift, int(centre_hz), (
            f"signal at {(freq_hz - sweet_hz) / 1000:.3f} kHz RF (LSB); dial "
            f"{-shift:+d} Hz so it sits at {centre_hz} Hz audio")
    return freq_hz, int(sweet_hz), "no sideband offset for this mode: dial as entered, carrier on the sweet spot"


def execute_entry(host: str, port: int, e: Entry, label: str, sweet_base: int,
                  switch_timeout: float, rpc_timeout: float, log: Log,
                  sleep: Callable[[float], None] = time.sleep) -> bool:
    """Apply one schedule entry. Order: rig mode, frequency, modem, carrier.
    Rig mode goes first because many radios move the VFO on a mode change.
    Never transmits. Returns True if every step verified."""
    p = make_proxy(host, port, rpc_timeout)
    problems = 0
    log("info", f"▶ {label}: starting")

    # 1. Rig mode
    if e.rig_mode and e.rig_mode != NO_CHANGE:
        try:
            try:
                known = list(p.rig.get_modes())
            except Exception:
                known = []
            if known and e.rig_mode not in known:
                log("warn", f"  rig mode '{e.rig_mode}' not in fldigi's rig mode list "
                            f"{known}; trying anyway")
            p.rig.set_mode(e.rig_mode)
            got = p.rig.get_mode()
            if got == e.rig_mode:
                log("ok", f"  rig mode → {got}")
            else:
                problems += 1
                log("warn", f"  rig mode requested {e.rig_mode}, fldigi reports '{got}'")
        except Exception as ex:
            problems += 1
            log("error", f"  rig mode failed: {describe_error(ex)}")

    # 2. Work out the sideband, then the dial and carrier
    sideband = rig_sideband(e.rig_mode)
    if sideband is None:
        try:
            sideband = rig_sideband(p.rig.get_mode())
        except Exception:
            sideband = None
    if sideband is None:
        try:
            sideband = rig_sideband(p.main.get_wf_sideband())
        except Exception:
            sideband = None
    khz = parse_freq_khz(e.freq_khz)
    dial, carrier, why = plan_tuning(None if khz is None else float(round(khz * 1000)),
                                     int(e.sweet_spot_hz), int(sweet_base), sideband)
    log("info", f"  {why}")
    if dial is not None:
        try:
            hz = float(round(dial))
            p.main.set_frequency(hz)
            got = get_dial_hz(p)
            if abs(got - hz) < 1.0:
                log("ok", f"  dial → {got / 1000:.3f} kHz")
            else:
                problems += 1
                log("warn", f"  frequency requested {hz / 1000:.3f} kHz, "
                            f"fldigi reports {got / 1000:.3f} kHz")
        except Exception as ex:
            problems += 1
            log("error", f"  frequency failed: {describe_error(ex)}")

    # 3. Digital mode (modem)
    if e.digital_mode and e.digital_mode != NO_CHANGE:
        try:
            cur = p.modem.get_name()
            if cur == e.digital_mode:
                log("ok", f"  modem already {cur}")
            else:
                p.modem.set_by_name(e.digital_mode)
                deadline = time.monotonic() + switch_timeout
                got = cur
                while time.monotonic() < deadline:
                    sleep(0.2)
                    got = p.modem.get_name()
                    if got == e.digital_mode:
                        break
                if got == e.digital_mode:
                    sleep(0.5)  # let modem init (and fldigi's own sweet spot) finish
                    log("ok", f"  modem {cur} → {got}")
                else:
                    problems += 1
                    log("error", f"  modem switch to {e.digital_mode} did not take; "
                                 f"fldigi is on {got}")
        except Exception as ex:
            problems += 1
            log("error", f"  modem switch failed: {describe_error(ex)}")

    # 4. Audio carrier (always applied, so a same-mode entry still recentres)
    try:
        p.modem.set_carrier(carrier)
        sleep(0.2)
        got = p.modem.get_carrier()
        if abs(got - carrier) <= 1:
            log("ok", f"  carrier → {got} Hz")
        else:
            problems += 1
            log("warn", f"  carrier requested {carrier} Hz, fldigi reports {got} Hz")
    except Exception as ex:
        problems += 1
        log("error", f"  carrier failed: {describe_error(ex)}")

    if problems:
        log("warn", f"■ {label}: finished with {problems} problem(s)")
    else:
        log("ok", f"■ {label}: done")
    return problems == 0
