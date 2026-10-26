#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 jeffreycentex
"""FLDigi Net Scheduler - PySide6 GUI.

Connects to fldigi's XML-RPC server, holds a weekly schedule of nets, and at
each scheduled UTC minute sets rig mode, dial frequency, modem and carrier.
Optionally connects an SDR and records the net through the KiwiRemote /
Icom client API, and ends both when the net's duration runs out.
It never keys the transmitter.

Run:   python3 fldigi_net_scheduler.py [--config PATH]
Needs: pip install PySide6   (sched_core.py must be in the same folder)
"""
from __future__ import annotations

import argparse
import dataclasses
import html
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QByteArray, QObject, QRegularExpression, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QFontDatabase, QKeySequence, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QSpinBox, QSplitter, QStyledItemDelegate,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import sched_core as core
from sched_core import DAYS, NO_CHANGE, Entry

APP_NAME = "FLDigi Net Scheduler"
APP_ID = "FLDigiNetScheduler"

(COL_ON, COL_NAME, COL_DAY, COL_TIME, COL_LOCAL,
 COL_FREQ, COL_MODE, COL_DIGI, COL_SWEET, COL_SDR, COL_REC, COL_DUR) = range(12)
HEADERS = ["On", "Net / label", "Day (UTC)", "Start UTC", "Local start",
           "Freq (kHz)", "Rig mode", "Digital mode", "Sweet spot (Hz)",
           "SDR", "Record", "Duration"]
VISIBLE_ROWS = 5
ROW_H = 28
HEADER_TIPS = {
    COL_ON: "Rows set to Off are kept but never run by the scheduler",
    COL_DAY: "Day of the week in UTC (a 01:00 UTC Tuesday net is Monday evening in the Americas)",
    COL_TIME: "24-hour UTC, HH:MM (0100 and 1:00 are accepted)",
    COL_LOCAL: "Next start converted to this computer's time zone (read-only)",
    COL_FREQ: "Dial (VFO) frequency in kHz, e.g. 7072 or 14070.5. Blank = don't change",
    COL_MODE: "Rig mode sent with rig.set_mode",
    COL_DIGI: "fldigi modem sent with modem.set_by_name",
    COL_SWEET: ("The net's sweet spot. The dial sent to fldigi is reduced by "
                "(FLDigi PSK Sweet Spot - this value). "
                "Example: 7110 kHz, sweet spot 900, FLDigi 1500 -> dial 7109.400 kHz, carrier 1500"),
    COL_SDR: ("SDR server for this net: Off, the Default SDR, or a custom server and password. "
              "Connected at the start through the client API, disconnected at the end"),
    COL_REC: "Record the net through the client API (file: net name without spaces + UTC date/time)",
    COL_DUR: "How long the net runs. At the end the recording stops and a connected SDR is disconnected",
}
BAD_STYLE = "background-color: rgb(255, 205, 205);"
LEVEL_COLORS = {"info": None, "ok": "#2e7d32", "warn": "#b26a00", "error": "#c62828"}
INVALID_BG = QColor(255, 205, 205)


# ---------------------------------------------------------------- widgets

class NoWheelComboBox(QComboBox):
    """Ignore the scroll wheel unless focused, so scrolling the grid can't
    silently change a cell."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, ev):
        if self.hasFocus():
            super().wheelEvent(ev)
        else:
            ev.ignore()


class NoWheelSpinBox(QSpinBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, ev):
        if self.hasFocus():
            super().wheelEvent(ev)
        else:
            ev.ignore()


class SdrDialog(QDialog):
    """Pick Off / Default / Custom for a row, or (default_only) edit the
    Default SDR itself."""
    def __init__(self, parent, mode="off", server="", password="", default_server="",
                 default_only=False):
        super().__init__(parent)
        self.setWindowTitle("Default SDR" if default_only else "SDR for this net")
        self.default_only = default_only
        v = QVBoxLayout(self)
        self.group = QButtonGroup(self)
        self.r_off = QRadioButton("Off - don't connect an SDR (use this for the Icom client)")
        dflt = default_server or "not set - use the Default SDR… button"
        self.r_def = QRadioButton(f"Use the Default SDR ({dflt})")
        self.r_cus = QRadioButton("Custom server:")
        if not default_only:
            for i, r in enumerate((self.r_off, self.r_def, self.r_cus)):
                self.group.addButton(r, i)
                v.addWidget(r)
        form = QFormLayout()
        self.server = QLineEdit(server)
        self.server.setPlaceholderText("host:port, e.g. kiwi.example.net:8073")
        self.server.setMinimumWidth(420)
        self.password = QLineEdit(password)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("optional")
        self.show_pw = QCheckBox("Show")
        self.show_pw.toggled.connect(lambda on: self.password.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        pw_row = QHBoxLayout()
        pw_row.addWidget(self.password, 1)
        pw_row.addWidget(self.show_pw)
        form.addRow("Server", self.server)
        form.addRow("Password", pw_row)
        v.addLayout(form)
        note = QLabel("The password is stored in plain text in the schedule file.")
        note.setStyleSheet("color: gray;")
        v.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        if not default_only:
            {"default": self.r_def, "custom": self.r_cus}.get(mode, self.r_off).setChecked(True)
            self.group.idToggled.connect(lambda *_: self._sync())
            self._sync()
        self.resize(max(600, self.sizeHint().width()), self.sizeHint().height())

    def _sync(self):
        on = self.default_only or self.r_cus.isChecked()
        for w in (self.server, self.password, self.show_pw):
            w.setEnabled(on)

    def _accept(self):
        # A blank Default SDR is allowed (it clears the default); Custom needs a server.
        if not self.default_only and self.r_cus.isChecked() and not self.server.text().strip():
            QMessageBox.warning(self, self.windowTitle(), "Enter a server (host:port).")
            return
        self.accept()

    def mode(self) -> str:
        if self.r_cus.isChecked():
            return "custom"
        return "default" if self.r_def.isChecked() else "off"


class SdrButton(QPushButton):
    """Cell button holding a row's SDR choice."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.mode, self.server, self.password = "off", "", ""
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_value(self, mode: str, server: str, password: str):
        self.mode, self.server, self.password = mode, server, password
        self.refresh()

    def refresh(self):
        self.setText({"off": "Off", "default": "Default"}.get(self.mode, self.server or "Custom?"))
        self.setToolTip({"off": "No SDR", "default": "Uses the Default SDR"}.get(
            self.mode, f"Custom: {self.server}" + (" (with password)" if self.password else "")))


class RegexDelegate(QStyledItemDelegate):
    def __init__(self, pattern: str, placeholder: str, parent=None):
        super().__init__(parent)
        self._re = QRegularExpression(pattern)
        self._ph = placeholder

    def createEditor(self, parent, option, index):
        ed = QLineEdit(parent)
        ed.setValidator(QRegularExpressionValidator(self._re, ed))
        ed.setPlaceholderText(self._ph)
        return ed


def set_combo_items(combo: QComboBox, items: list[str], keep: str | None = None):
    cur = keep if keep is not None else combo.currentText()
    combo.blockSignals(True)
    combo.clear()
    combo.addItems([NO_CHANGE] + [i for i in items if i != NO_CHANGE])
    if cur and combo.findText(cur) < 0:
        combo.addItem(cur)          # keep a saved value fldigi doesn't list
    combo.setCurrentText(cur or NO_CHANGE)
    combo.blockSignals(False)


# ---------------------------------------------------------------- worker

class Bridge(QObject):
    """Signals emitted from the worker thread, delivered on the GUI thread."""
    log = Signal(str, str)
    status = Signal(dict)
    lists = Signal(list, list)
    kiwi = Signal(dict)          # client API status
    active = Signal(dict)        # ActiveNet as dict after a start ({} = nothing to end)


class Worker:
    """One background thread, so XML-RPC calls never block the GUI and
    never overlap each other."""
    def __init__(self, bridge: Bridge):
        self.b = bridge
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fldigi")

    def _run(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:  # last-resort guard; individual steps log their own errors
            self.b.log.emit("error", f"{getattr(fn, '__name__', 'task')}: {core.describe_error(e)}")

    def poll(self, host, port, timeout):
        self.pool.submit(self._run, lambda: self.b.status.emit(core.poll_status(host, port, timeout)))

    def refresh_lists(self, host, port, timeout):
        def task():
            rig, modems = core.fetch_lists(host, port, timeout)
            self.b.lists.emit(rig, modems)
            self.b.log.emit("info", f"fldigi reports {len(modems)} modems and "
                                    f"{len(rig)} rig modes")
        self.pool.submit(self._run, task)

    def start_net(self, st: core.Settings, entry: Entry, label: str, previous):
        """st is a snapshot of the settings taken on the GUI thread."""
        def task():
            api = core.KiwiApi(st.api_host, st.api_port, st.api_token)
            fl = lambda: core.execute_entry(st.host, st.port, entry, label, st.sweet_spot_base_hz,
                                            st.mode_switch_timeout_s, st.xmlrpc_timeout_s,
                                            self.b.log.emit)
            a = core.start_net(api, entry, label, st, fl, self.b.log.emit, previous)
            self.b.active.emit(a.to_dict() if a else {})
        self.pool.submit(self._run, task)

    def end_net(self, st: core.Settings, active, reason):
        api = core.KiwiApi(st.api_host, st.api_port, st.api_token)
        self.pool.submit(self._run, core.end_net, api, active, self.b.log.emit, reason)

    def poll_kiwi(self, st: core.Settings):
        api = core.KiwiApi(st.api_host, st.api_port, st.api_token, timeout=2.0)
        self.pool.submit(self._run, lambda: self.b.kiwi.emit(core.api_status_summary(api)))

    def shutdown(self):
        self.pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    def __init__(self, config_path: str):
        super().__init__()
        self.config_path = config_path
        self.settings = core.Settings()
        self.rig_modes = list(core.DEFAULT_RIG_MODES)
        self.digital_modes = list(core.DEFAULT_DIGITAL_MODES)
        self._loading = False
        self._poll_pending = False
        self._connected = None          # None = unknown, then True/False
        self._kiwi_ok = None
        self._kiwi_pending = False
        self.active: core.ActiveNet | None = None   # net running now
        self._starting = False                       # a start is queued in the worker
        self._stop_requested = False                 # Stop pressed while starting

        self.bridge = Bridge()
        self.bridge.log.connect(self.log)
        self.bridge.status.connect(self._on_status)
        self.bridge.lists.connect(self._on_lists)
        self.bridge.kiwi.connect(self._on_kiwi)
        self.bridge.active.connect(self._on_active)
        self.worker = Worker(self.bridge)

        self.autosave_timer = QTimer(self)
        self.autosave_timer.setSingleShot(True)
        self.autosave_timer.setInterval(800)
        self.autosave_timer.timeout.connect(self._autosave)

        self._build_ui()
        self._load(self.config_path, initial=True)

        self.tracker = core.MinuteTracker(core.utcnow())
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self._on_clock)
        self.clock_timer.start(250)
        self.minute_timer = QTimer(self)
        self.minute_timer.setSingleShot(True)
        self.minute_timer.timeout.connect(self._on_minute)
        self._arm_minute_timer()
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(lambda: (self._poll(), self._poll_kiwi()))
        self.poll_timer.start(max(2, self.settings.poll_interval_s) * 1000)

        self._on_clock()
        self._connect()
        self._poll_kiwi()
        # Run anything due in the current minute right away.
        QTimer.singleShot(1500, self._on_minute)

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        self.setWindowTitle(APP_NAME)
        fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)

        # connection row
        conn_box = QGroupBox("fldigi XML-RPC")
        cl = QHBoxLayout(conn_box)
        self.host_edit = QLineEdit(core.DEFAULT_HOST)
        self.host_edit.setMinimumWidth(200)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(core.DEFAULT_PORT)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(lambda *_: self._connect())
        self.host_edit.returnPressed.connect(lambda: self._connect())
        self.base_spin = QSpinBox()
        self.base_spin.setRange(300, 3000)
        self.base_spin.setSuffix(" Hz")
        self.base_spin.setValue(core.DEFAULT_SWEET_BASE)
        self.base_spin.setToolTip("fldigi's PSK sweet spot. The carrier is set here and each row's "
                                  "dial is reduced by (this - row sweet spot)")
        self.base_spin.valueChanged.connect(self._on_widget_changed)
        self.status_lbl = QLabel("● not connected")
        self.status_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.status_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for w in (QLabel("Host"), self.host_edit, QLabel("Port"), self.port_spin, self.connect_btn,
                  QLabel("   FLDigi PSK Sweet Spot"), self.base_spin):
            cl.addWidget(w)
        cl.addSpacing(12)
        cl.addWidget(self.status_lbl, 1)

        # client API row
        api_box = QGroupBox("SDR / recording client API (KiwiRemote or Icom client)")
        al = QHBoxLayout(api_box)
        self.api_host_edit = QLineEdit(core.DEFAULT_API_HOST)
        self.api_host_edit.setMinimumWidth(200)
        self.api_port_spin = QSpinBox()
        self.api_port_spin.setRange(1, 65535)
        self.api_port_spin.setValue(core.DEFAULT_API_PORT)
        self.api_token_edit = QLineEdit()
        self.api_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_token_edit.setPlaceholderText("optional")
        self.api_token_edit.setMaximumWidth(140)
        for w in (self.api_host_edit, self.api_token_edit):
            w.editingFinished.connect(self._on_api_changed)
        self.api_port_spin.valueChanged.connect(self._on_api_changed)
        self.default_sdr_btn = QPushButton("Default SDR…")
        self.default_sdr_btn.setMinimumWidth(300)
        self.default_sdr_btn.clicked.connect(lambda *_: self._edit_default_sdr())
        self.kiwi_lbl = QLabel("● client API: not checked yet")
        self.kiwi_lbl.setTextFormat(Qt.TextFormat.RichText)
        for w in (QLabel("Host"), self.api_host_edit, QLabel("Port"), self.api_port_spin,
                  QLabel("Token"), self.api_token_edit, self.default_sdr_btn):
            al.addWidget(w)
        al.addSpacing(12)
        al.addWidget(self.kiwi_lbl, 1)

        # clocks
        clock_box = QGroupBox("Time")
        kl = QHBoxLayout(clock_box)
        big = QFont(fixed)
        big.setPointSize(max(big.pointSize() + 6, 16))
        self.local_lbl = QLabel()
        self.utc_lbl = QLabel()
        for lbl in (self.local_lbl, self.utc_lbl):
            lbl.setFont(big)
        kl.addWidget(QLabel("Local"))
        kl.addWidget(self.local_lbl)
        kl.addSpacing(30)
        kl.addWidget(QLabel("UTC"))
        kl.addWidget(self.utc_lbl)
        kl.addStretch(1)

        # toolbar row
        tb = QHBoxLayout()
        def btn(text, slot, tip=""):
            b = QPushButton(text)
            b.clicked.connect(lambda *_, f=slot: f())
            b.setToolTip(tip)
            tb.addWidget(b)
            return b
        btn("Add row", self._add_row)
        btn("Insert row", self._insert_row, "Insert above the selected row")
        btn("Delete rows", self._delete_rows, "Delete the selected rows")
        btn("▲", lambda: self._move(-1), "Move selected row up")
        btn("▼", lambda: self._move(+1), "Move selected row down")
        btn("Sort by day/time", self._sort)
        tb.addSpacing(16)
        btn("Run selected now", self._run_selected,
            "Start the selected net now: SDR, fldigi, recording, and stop after its duration")
        self.stop_btn = btn("Stop active net", self._stop_active,
                            "Stop the recording and disconnect the SDR of the net running now")
        btn("Refresh mode lists", self._refresh_lists,
            "Re-read rig.get_modes and modem.get_names from fldigi")
        tb.addStretch(1)
        self.sched_chk = QCheckBox("Scheduler enabled")
        self.sched_chk.setChecked(True)
        self.sched_chk.toggled.connect(self._on_sched_toggled)
        tb.addWidget(self.sched_chk)

        # grid
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        for col, tip in HEADER_TIPS.items():
            self.table.horizontalHeaderItem(col).setToolTip(tip)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setItemDelegateForColumn(
            COL_TIME, RegexDelegate(r"^\d{0,2}:?\d{0,2}$", "HH:MM", self.table))
        self.table.setItemDelegateForColumn(
            COL_FREQ, RegexDelegate(r"^\d{0,7}([.,]\d{0,3})?$", "kHz", self.table))
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        for col, w in ((COL_ON, 64), (COL_DAY, 100), (COL_TIME, 80), (COL_LOCAL, 110),
                       (COL_FREQ, 90), (COL_MODE, 100), (COL_DIGI, 130), (COL_SWEET, 95),
                       (COL_SDR, 230), (COL_REC, 64), (COL_DUR, 90)):
            self.table.setColumnWidth(col, w)
        self.table.verticalHeader().setDefaultSectionSize(ROW_H)
        self.table.itemChanged.connect(self._on_item_changed)
        # room for VISIBLE_ROWS rows; more rows scroll
        h = (hh.sizeHint().height() + VISIBLE_ROWS * ROW_H + 2 * self.table.frameWidth()
             + self.table.horizontalScrollBar().sizeHint().height() + 4)
        self.table.setMinimumHeight(h)
        self.table.setMaximumHeight(h)

        self.next_lbl = QLabel()
        self.next_lbl.setFont(fixed)
        self.active_lbl = QLabel()
        self.active_lbl.setFont(fixed)
        self.active_lbl.setTextFormat(Qt.TextFormat.RichText)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))

        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addLayout(tb)
        tl.addWidget(self.table)
        tl.addWidget(self.next_lbl)
        tl.addWidget(self.active_lbl)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(top)
        split.addWidget(self.log_view)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setChildrenCollapsible(False)

        central = QWidget()
        v = QVBoxLayout(central)
        v.addWidget(conn_box)
        v.addWidget(api_box)
        v.addWidget(clock_box)
        v.addWidget(split, 1)
        self.setCentralWidget(central)
        self.resize(1580, 840)

        # menu
        m = self.menuBar().addMenu("&File")
        for text, key, slot in (("&Open…", QKeySequence.StandardKey.Open, self._open),
                                ("&Save", QKeySequence.StandardKey.Save, self._save_now),
                                ("Save &As…", QKeySequence.StandardKey.SaveAs, self._save_as),
                                (None, None, None),
                                ("&Quit", QKeySequence.StandardKey.Quit, self.close)):
            if text is None:
                m.addSeparator()
                continue
            a = QAction(text, self)
            a.setShortcut(key)
            a.triggered.connect(lambda *_, f=slot: f())
            m.addAction(a)
        self.path_lbl = QLabel()
        self.statusBar().addPermanentWidget(self.path_lbl)

    # ------------------------------------------------------------ rows
    def _init_row(self, r: int):
        t = self.table
        holder = QTableWidgetItem("")   # keeps the cell selectable under the widget
        holder.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        t.setItem(r, COL_ON, holder)
        on = NoWheelComboBox()
        on.addItems(["On", "Off"])
        on.setCurrentText("Off")
        on.currentTextChanged.connect(self._on_widget_changed)
        t.setCellWidget(r, COL_ON, on)
        t.setItem(r, COL_NAME, QTableWidgetItem(""))
        t.setItem(r, COL_TIME, QTableWidgetItem(""))
        loc = QTableWidgetItem("")
        loc.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        loc.setForeground(QBrush(QColor(110, 110, 110)))
        t.setItem(r, COL_LOCAL, loc)
        t.setItem(r, COL_FREQ, QTableWidgetItem(""))

        day = NoWheelComboBox()
        day.addItems(DAYS)
        day.setCurrentText("Mon")
        day.currentTextChanged.connect(self._on_widget_changed)
        t.setCellWidget(r, COL_DAY, day)

        mode = NoWheelComboBox()
        set_combo_items(mode, self.rig_modes, "USB")
        mode.currentTextChanged.connect(self._on_widget_changed)
        t.setCellWidget(r, COL_MODE, mode)

        digi = NoWheelComboBox()
        set_combo_items(digi, self.digital_modes, NO_CHANGE)
        digi.currentTextChanged.connect(self._on_widget_changed)
        t.setCellWidget(r, COL_DIGI, digi)

        sweet = NoWheelSpinBox()
        sweet.setRange(core.SWEET_MIN, core.SWEET_MAX)
        sweet.setSingleStep(10)
        sweet.setSuffix(" Hz")
        sweet.setValue(core.DEFAULT_SWEET_BASE)
        sweet.valueChanged.connect(self._on_widget_changed)
        t.setCellWidget(r, COL_SWEET, sweet)

        sdr = SdrButton()
        sdr.refresh()
        sdr.clicked.connect(lambda *_, b=sdr: self._edit_row_sdr(b))
        t.setCellWidget(r, COL_SDR, sdr)

        rec = NoWheelComboBox()
        rec.addItems(["Off", "On"])
        rec.currentTextChanged.connect(self._on_widget_changed)
        t.setCellWidget(r, COL_REC, rec)

        dur = NoWheelSpinBox()
        dur.setRange(0, core.MAX_DURATION_MIN)
        dur.setSingleStep(5)
        dur.setSuffix(" min")
        dur.setSpecialValueText("none")
        dur.setValue(core.DEFAULT_DURATION_MIN)
        dur.valueChanged.connect(self._on_widget_changed)
        t.setCellWidget(r, COL_DUR, dur)

    def _get_row(self, r: int) -> Entry:
        t = self.table
        return Entry(
            enabled=t.cellWidget(r, COL_ON).currentText() == "On",
            name=t.item(r, COL_NAME).text().strip(),
            day=t.cellWidget(r, COL_DAY).currentText(),
            start_utc=t.item(r, COL_TIME).text().strip(),
            freq_khz=t.item(r, COL_FREQ).text().strip(),
            rig_mode=t.cellWidget(r, COL_MODE).currentText(),
            digital_mode=t.cellWidget(r, COL_DIGI).currentText(),
            sweet_spot_hz=t.cellWidget(r, COL_SWEET).value(),
            sdr_mode=t.cellWidget(r, COL_SDR).mode,
            sdr_server=t.cellWidget(r, COL_SDR).server,
            sdr_password=t.cellWidget(r, COL_SDR).password,
            record=t.cellWidget(r, COL_REC).currentText() == "On",
            duration_min=t.cellWidget(r, COL_DUR).value(),
        )

    def _set_row(self, r: int, e: Entry):
        t = self.table
        was = self._loading
        self._loading = True
        try:
            on = t.cellWidget(r, COL_ON)
            on.blockSignals(True)
            on.setCurrentText("On" if e.enabled else "Off")
            on.blockSignals(False)
            t.item(r, COL_NAME).setText(e.name)
            t.item(r, COL_TIME).setText(core.normalize_time(e.start_utc) or e.start_utc)
            t.item(r, COL_FREQ).setText(e.freq_khz)
            day = t.cellWidget(r, COL_DAY)
            day.blockSignals(True)
            day.setCurrentText(e.day if e.day in DAYS else "Mon")
            day.blockSignals(False)
            set_combo_items(t.cellWidget(r, COL_MODE), self.rig_modes, e.rig_mode or NO_CHANGE)
            set_combo_items(t.cellWidget(r, COL_DIGI), self.digital_modes, e.digital_mode or NO_CHANGE)
            sw = t.cellWidget(r, COL_SWEET)
            sw.blockSignals(True)
            sw.setValue(max(core.SWEET_MIN, min(core.SWEET_MAX, int(e.sweet_spot_hz))))
            sw.blockSignals(False)
            t.cellWidget(r, COL_SDR).set_value(e.sdr_mode, e.sdr_server, e.sdr_password)
            rec = t.cellWidget(r, COL_REC)
            rec.blockSignals(True)
            rec.setCurrentText("On" if e.record else "Off")
            rec.blockSignals(False)
            dur = t.cellWidget(r, COL_DUR)
            dur.blockSignals(True)
            dur.setValue(max(0, min(core.MAX_DURATION_MIN, int(e.duration_min))))
            dur.blockSignals(False)
        finally:
            self._loading = was

    def entries(self) -> list[Entry]:
        return [self._get_row(r) for r in range(self.table.rowCount())]

    def _set_entries(self, entries: list[Entry]):
        self._loading = True
        try:
            self.table.setRowCount(0)
            n = max(core.MIN_ROWS, len(entries))
            self.table.setRowCount(n)
            for r in range(n):
                self._init_row(r)
                self._set_row(r, entries[r] if r < len(entries) else Entry())
        finally:
            self._loading = False
        self._refresh_derived()

    def _selected_rows(self) -> list[int]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        return rows

    def _new_row_at(self, r: int):
        self._loading = True
        try:
            self.table.insertRow(r)
            self._init_row(r)
        finally:
            self._loading = False
        self._refresh_derived()
        self.table.selectRow(r)
        self._mark_dirty()

    def _add_row(self):
        self._new_row_at(self.table.rowCount())
        self.table.scrollToBottom()

    def _insert_row(self):
        rows = self._selected_rows()
        self._new_row_at(rows[0] if rows else 0)

    def _delete_rows(self):
        rows = self._selected_rows()
        if not rows:
            return
        used = [r for r in rows if not self._get_row(r).is_blank()]
        if used and QMessageBox.question(
                self, APP_NAME, f"Delete {len(rows)} row(s)?") != QMessageBox.StandardButton.Yes:
            return
        keep = [e for i, e in enumerate(self.entries()) if i not in rows]
        self._set_entries(keep)
        self._mark_dirty()

    def _move(self, delta: int):
        rows = self._selected_rows()
        if len(rows) != 1:
            return
        r, s = rows[0], rows[0] + delta
        if not 0 <= s < self.table.rowCount():
            return
        a, b = self._get_row(r), self._get_row(s)
        self._set_row(r, b)
        self._set_row(s, a)
        self.table.selectRow(s)
        self._refresh_derived()
        self._mark_dirty()

    def _sort(self):
        es = self.entries()
        used = [e for e in es if not e.is_blank()]
        def key(e: Entry):
            t = core.normalize_time(e.start_utc) or "99:99"
            return (DAYS.index(e.day) if e.day in DAYS else 99, t, e.name.lower())
        self._set_entries(sorted(used, key=key))
        self._mark_dirty()

    # ------------------------------------------------------------ change handling
    def _on_item_changed(self, item: QTableWidgetItem):
        if self._loading:
            return
        if item.column() == COL_TIME:
            norm = core.normalize_time(item.text())
            if norm and norm != item.text():
                self._loading = True
                item.setText(norm)
                self._loading = False
        elif item.column() == COL_FREQ:
            txt = item.text().strip().replace(",", ".")
            if txt != item.text():
                self._loading = True
                item.setText(txt)
                self._loading = False
        self._refresh_derived()
        self._mark_dirty()

    def _on_widget_changed(self, *_):
        if self._loading:
            return
        self._refresh_derived()
        self._mark_dirty()

    def _refresh_derived(self):
        """Recompute the Local column and validation colouring."""
        now = core.utcnow()
        self._loading = True
        try:
            for r in range(self.table.rowCount()):
                e = self._get_row(r)
                t_item, f_item = self.table.item(r, COL_TIME), self.table.item(r, COL_FREQ)
                bad_t = bool(e.start_utc) and core.normalize_time(e.start_utc) is None
                bad_t = bad_t or (not e.start_utc and not e.is_blank())
                bad_f = bool(e.freq_khz) and core.parse_freq_khz(e.freq_khz) is None
                t_item.setBackground(QBrush(INVALID_BG) if bad_t else QBrush())
                f_item.setBackground(QBrush(INVALID_BG) if bad_f else QBrush())
                t_item.setToolTip("Required: HH:MM 24-hour UTC" if bad_t else "")
                f_item.setToolTip("Number in kHz, e.g. 14070.5" if bad_f else "")
                self.table.item(r, COL_LOCAL).setText(core.local_start_text(e, now))
                self.table.cellWidget(r, COL_SWEET).setToolTip(self._tuning_text(e))
                probs = [] if e.is_blank() else e.problems(self.settings)
                sdr_bad = any("SDR" in p for p in probs)
                dur_bad = any("duration" in p for p in probs)
                sdr_btn = self.table.cellWidget(r, COL_SDR)
                sdr_btn.setStyleSheet(BAD_STYLE if sdr_bad else "")
                if sdr_bad:
                    sdr_btn.setToolTip("; ".join(p for p in probs if "SDR" in p))
                else:
                    sdr_btn.refresh()
                dur_w = self.table.cellWidget(r, COL_DUR)
                dur_w.setStyleSheet(BAD_STYLE if dur_bad else "")
                dur_w.setToolTip("Required when SDR or Record is used" if dur_bad else "")
        finally:
            self._loading = False

    def _tuning_text(self, e: Entry) -> str:
        khz = core.parse_freq_khz(e.freq_khz)
        dial, car, why = core.plan_tuning(None if khz is None else khz * 1000, e.sweet_spot_hz,
                                          self.base_spin.value())
        d = f"dial {dial / 1000:.3f} kHz, " if dial is not None else ""
        return f"{d}carrier {car} Hz - {why}"

    def _edit_row_sdr(self, b: SdrButton):
        d = SdrDialog(self, b.mode, b.server, b.password, self.settings.default_sdr_server)
        if d.exec():
            m = d.mode()
            b.set_value(m, d.server.text().strip() if m == "custom" else b.server,
                        d.password.text() if m == "custom" else b.password)
            self._on_widget_changed()

    def _edit_default_sdr(self):
        s = self.settings
        d = SdrDialog(self, server=s.default_sdr_server, password=s.default_sdr_password,
                      default_only=True)
        if d.exec():
            s.default_sdr_server = d.server.text().strip()
            s.default_sdr_password = d.password.text()
            self._refresh_default_btn()
            self._refresh_derived()
            self._mark_dirty()
            self.log("info", f"Default SDR set to {s.default_sdr_server or '(none)'}")

    def _refresh_default_btn(self):
        srv = self.settings.default_sdr_server
        self.default_sdr_btn.setText(f"Default SDR: {srv}" if srv else "Default SDR…")
        self.default_sdr_btn.setToolTip("Server used by rows whose SDR is set to Default"
                                        + (" (with password)" if self.settings.default_sdr_password else ""))

    def _on_api_changed(self, *_):
        if self._loading:
            return
        self._sync_settings()
        self._kiwi_ok = None
        self._poll_kiwi()
        self._mark_dirty()

    def _mark_dirty(self, *_):
        if not self._loading:
            self.autosave_timer.start()

    def _on_sched_toggled(self, on: bool):
        self.log("info", "Scheduler enabled" if on else "Scheduler paused")
        self._mark_dirty()

    # ------------------------------------------------------------ clock & scheduler
    def _on_clock(self):
        now_l = datetime.now().astimezone()
        now_u = now_l.astimezone(timezone.utc)
        self.local_lbl.setText(now_l.strftime("%a %Y-%m-%d %H:%M:%S ") + (now_l.tzname() or ""))
        self.utc_lbl.setText(now_u.strftime("%a %Y-%m-%d %H:%M:%S"))
        self._update_next_label(now_u)
        self._check_active(now_u)

    def _update_next_label(self, now_u: datetime):
        if not self.sched_chk.isChecked():
            self.next_lbl.setText("Scheduler paused - nothing will run automatically.")
            return
        best = None
        for i, e in enumerate(self.entries()):
            if not e.is_schedulable(self.settings):
                continue
            occ = core.next_occurrence(e, now_u)
            if occ and (best is None or occ < best[0]):
                best = (occ, i, e)
        if not best:
            self.next_lbl.setText("Next: nothing scheduled.")
            return
        occ, i, e = best
        secs = int((occ - now_u).total_seconds())
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        left = (f"{d}d " if d else "") + f"{h:02d}:{m:02d}:{s:02d}"
        loc = occ.astimezone()
        self.next_lbl.setText(
            f"Next: {e.label(i)}  {occ:%a %H:%M} UTC  ({loc:%a %H:%M} local)  in {left}  "
            f"→ {e.rig_mode} {e.digital_mode}, {self._tuning_text(e).split(' - ')[0]}"
            + (f", SDR {e.sdr_target(self.settings)[0]}" if e.sdr_target(self.settings) else "")
            + (", REC" if e.record else "") + (f", {e.duration_min} min" if e.needs_end() else ""))

    def _check_active(self, now_u: datetime):
        a = self.active
        if a is None:
            self.active_lbl.setText("Active: none" + (" (starting…)" if self._starting else ""))
            self.stop_btn.setEnabled(self._starting)
            return
        self.stop_btn.setEnabled(True)
        end = a.end_dt()
        if now_u >= end:
            self._end_active("duration ended")
            return
        left = int((end - now_u).total_seconds())
        h, rem = divmod(left, 3600)
        m, sec = divmod(rem, 60)
        bits = []
        if a.recording:
            bits.append("<span style='color:#c62828'>● REC</span>")
        if a.used_sdr:
            bits.append(f"SDR {html.escape(a.server)}")
        self.active_lbl.setText(
            f"Active: <b>{html.escape(a.label)}</b> until {end:%H:%M} UTC "
            f"({h:d}:{m:02d}:{sec:02d} left)  " + "  ".join(bits))

    def _end_active(self, reason: str):
        a, self.active = self.active, None
        if a is None:
            return
        self._sync_settings()
        self.worker.end_net(dataclasses.replace(self.settings), a, reason)
        self._mark_dirty()

    def _stop_active(self):
        if self.active is None and self._starting:
            self._stop_requested = True
            self.log("info", "Stop requested; the net will be stopped as soon as it has started")
            return
        if self.active is None:
            self.log("info", "No active net to stop")
            return
        self._end_active("stopped by user")

    def _arm_minute_timer(self):
        now = datetime.now()
        ms = int((60 - now.second - now.microsecond / 1e6) * 1000) + 300  # just past :00
        self.minute_timer.start(max(ms, 50))

    def _on_minute(self):
        try:
            minutes, skipped = self.tracker.advance(core.utcnow())
            if skipped:
                self.log("warn", f"Clock jumped or computer slept: skipped {skipped} minute(s) "
                                 f"of schedule checks")
            if minutes:
                self._refresh_derived()   # local column can change across DST
            if self.sched_chk.isChecked():
                entries = self.entries()
                for minute in minutes:
                    due = core.due_entries(entries, minute, self.settings)
                    if len(due) > 1:
                        self.log("warn", f"{len(due)} entries due at {minute:%a %H:%M} UTC; "
                                         f"running in grid order, the last one wins")
                    for i, e in due:
                        self.log("info", f"Scheduled {minute:%a %H:%M} UTC: {e.label(i)}")
                        self._execute(i, e)
        finally:
            self._arm_minute_timer()

    def _execute(self, row: int, e: Entry):
        """Start a net. Any net still active is ended first (in the worker)."""
        self._sync_settings()
        previous, self.active = self.active, None
        self._starting = True
        self.worker.start_net(dataclasses.replace(self.settings), e, e.label(row), previous)

    def _on_active(self, d: dict):
        self._starting = False
        self.active = core.ActiveNet.from_dict(d) if d else None
        if self._stop_requested:
            self._stop_requested = False
            self._end_active("stopped by user")
        self._mark_dirty()

    def _run_selected(self):
        rows = [r for r in self._selected_rows() if not self._get_row(r).is_blank()]
        if not rows:
            self.log("warn", "Select a non-empty row to run")
            return
        if len(rows) > 1:
            self.log("warn", "Only one net can run at a time; running the first selected row")
        r = rows[0]
        e = self._get_row(r)
        probs = e.problems(self.settings)
        if probs:
            self.log("error", f"{e.label(r)}: " + "; ".join(probs))
            return
        self.log("info", f"Manual run: {e.label(r)}")
        self._execute(r, e)

    # ------------------------------------------------------------ fldigi link
    def _connect(self):
        host = self.host_edit.text().strip() or core.DEFAULT_HOST
        self.host_edit.setText(host)
        self.log("info", f"Connecting to http://{host}:{self.port_spin.value()}/")
        self._connected = None
        self._poll(force=True)
        self._refresh_lists()
        self._mark_dirty()

    def _refresh_lists(self):
        self.worker.refresh_lists(self.host_edit.text().strip(), self.port_spin.value(),
                                  self.settings.xmlrpc_timeout_s)

    def _poll(self, force=False):
        if self._poll_pending and not force:
            return
        self._poll_pending = True
        self.worker.poll(self.host_edit.text().strip(), self.port_spin.value(),
                         self.settings.xmlrpc_timeout_s)

    def _poll_kiwi(self):
        if self._kiwi_pending:
            return
        self._kiwi_pending = True
        self._sync_settings()
        self.worker.poll_kiwi(dataclasses.replace(self.settings))

    def _on_kiwi(self, st: dict):
        self._kiwi_pending = False
        if st.get("ok"):
            txt = f"<span style='color:#2e7d32'>●</span> client {html.escape(st['state'])}"
            if st.get("server"):
                txt += f" │ {html.escape(st['server'])}"
            if st.get("recording"):
                m, sec = divmod(int(st.get("rec_seconds") or 0), 60)
                txt += f" │ <span style='color:#c62828'>REC {m}:{sec:02d}</span>"
            self.kiwi_lbl.setText(txt)
            if self._kiwi_ok is not True:
                self.log("ok", f"Client API reachable (app {st.get('app_version') or '?'})")
            self._kiwi_ok = True
        else:
            self.kiwi_lbl.setText(f"<span style='color:#888'>●</span> client API: "
                                  f"{html.escape(st.get('error', ''))}")
            if self._kiwi_ok is True:
                self.log("warn", f"Client API not reachable: {st.get('error')}")
            self._kiwi_ok = False

    def _on_status(self, st: dict):
        self._poll_pending = False
        if st.get("ok"):
            fr = st.get("freq_hz") or 0
            self.status_lbl.setText(
                f"<span style='color:#2e7d32'>●</span> fldigi {html.escape(str(st['version']))} │ "
                f"{fr / 1000:.3f} kHz {html.escape(st.get('rig_mode') or '')} │ "
                f"{html.escape(st['modem'])} @ {st['carrier']} Hz")
            if self._connected is not True:
                self.log("ok", f"Connected to fldigi {st['version']}")
                if self._connected is False:
                    self._refresh_lists()
            self._connected = True
        else:
            self.status_lbl.setText(
                f"<span style='color:#c62828'>●</span> not connected: {html.escape(st['error'])}")
            if self._connected is not False:
                self.log("error", f"fldigi not reachable: {st['error']}")
            self._connected = False

    def _on_lists(self, rig_modes: list, modems: list):
        if rig_modes:
            self.rig_modes = [str(m) for m in rig_modes]
        if modems:
            self.digital_modes = [str(m) for m in modems]
        self._loading = True
        try:
            for r in range(self.table.rowCount()):
                set_combo_items(self.table.cellWidget(r, COL_MODE), self.rig_modes)
                set_combo_items(self.table.cellWidget(r, COL_DIGI), self.digital_modes)
        finally:
            self._loading = False
        if not rig_modes:
            self.log("warn", "fldigi returned no rig modes (rig control not configured?); "
                             "using the built-in list")
        for i, e in enumerate(self.entries()):
            if not e.is_blank() and e.digital_mode != NO_CHANGE and e.digital_mode not in self.digital_modes:
                self.log("warn", f"{e.label(i)}: modem '{e.digital_mode}' is not in fldigi's list")

    # ------------------------------------------------------------ persistence
    def _sync_settings(self):
        """Copy the settings widgets into self.settings."""
        s = self.settings
        s.host = self.host_edit.text().strip() or core.DEFAULT_HOST
        s.port = self.port_spin.value()
        s.sweet_spot_base_hz = self.base_spin.value()
        s.scheduler_enabled = self.sched_chk.isChecked()
        s.api_host = self.api_host_edit.text().strip() or core.DEFAULT_API_HOST
        s.api_port = self.api_port_spin.value()
        s.api_token = self.api_token_edit.text().strip()
        s.active_net = self.active.to_dict() if self.active else {}

    def _document(self) -> core.Document:
        self._sync_settings()
        s = self.settings
        s.window_geometry = self.saveGeometry().toBase64().data().decode("ascii")
        return core.Document(s, self.entries())

    def _apply_document(self, doc: core.Document):
        self.settings = doc.settings
        s = doc.settings
        self._loading = True
        try:
            self.host_edit.setText(s.host)
            self.port_spin.setValue(s.port)
            self.base_spin.setValue(max(300, min(3000, s.sweet_spot_base_hz)))
            self.sched_chk.blockSignals(True)
            self.sched_chk.setChecked(s.scheduler_enabled)
            self.sched_chk.blockSignals(False)
            self.api_host_edit.setText(s.api_host)
            self.api_port_spin.setValue(s.api_port)
            self.api_token_edit.setText(s.api_token)
        finally:
            self._loading = False
        self._refresh_default_btn()
        self.active = core.ActiveNet.from_dict(s.active_net) if s.active_net else None
        if self.active:
            self.log("info", f"Resuming active net {self.active.label}: ends "
                             f"{self.active.end_dt():%H:%M} UTC")
        if s.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(s.window_geometry.encode("ascii")))
        self._set_entries(doc.entries)

    def _load(self, path: str, initial=False):
        doc = core.Document()
        if os.path.exists(path):
            try:
                doc = core.load_document(path)
                self.log("info", f"Loaded {len([e for e in doc.entries if not e.is_blank()])} "
                                 f"entries from {path}")
            except Exception as e:
                bad = f"{path}.bad-{datetime.now():%Y%m%d-%H%M%S}"
                try:
                    os.replace(path, bad)
                    msg = f"Could not read {path} ({e}). It was renamed to {bad}."
                except OSError:
                    msg = f"Could not read {path} ({e})."
                self.log("error", msg)
                QMessageBox.warning(self, APP_NAME, msg)
                if not initial:
                    return
        elif initial:
            self.log("info", f"No schedule yet; it will be saved to {path}")
        self.config_path = path
        self._apply_document(doc)
        self.path_lbl.setText(path)

    def _save(self, path: str) -> bool:
        try:
            core.save_document(self._document(), path)
            self.statusBar().showMessage(f"Saved {datetime.now():%H:%M:%S}", 4000)
            return True
        except Exception as e:
            self.log("error", f"Save failed: {e}")
            return False

    def _autosave(self):
        self._save(self.config_path)

    def _save_now(self):
        self.autosave_timer.stop()
        self._save(self.config_path)

    def _save_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save schedule", self.config_path, "JSON (*.json)")
        if path:
            if not path.lower().endswith(".json"):
                path += ".json"
            if self._save(path):
                self.config_path = path
                self.path_lbl.setText(path)

    def _open(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open schedule", os.path.dirname(self.config_path),
                                              "JSON (*.json)")
        if path:
            self._save_now()
            self._load(path)
            self._connect()

    # ------------------------------------------------------------ log & close
    def log(self, level: str, msg: str):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
        color = LEVEL_COLORS.get(level)
        text = html.escape(f"{ts}  {msg}")
        if color:
            text = f"<span style='color:{color}'>{text}</span>"
        self.log_view.appendHtml(f"<pre style='margin:0'>{text}</pre>")

    def closeEvent(self, ev):
        self.autosave_timer.stop()
        self._save(self.config_path)
        self.worker.shutdown()
        super().closeEvent(ev)


def default_config_path() -> str:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    return os.path.join(base or os.path.expanduser("~"), "schedule.json")


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--config", help="schedule JSON file (default: per-user app config folder)")
    args, qt_args = ap.parse_known_args()
    app = QApplication([sys.argv[0]] + qt_args)
    app.setApplicationName(APP_ID)
    app.setApplicationDisplayName(APP_NAME)
    w = MainWindow(os.path.abspath(args.config) if args.config else default_config_path())
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
