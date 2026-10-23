#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 jeffreycentex
"""FLDigi Net Scheduler - PySide6 GUI.

Connects to fldigi's XML-RPC server, holds a weekly schedule of nets, and at
each scheduled UTC minute sets rig mode, dial frequency, modem and carrier.
It never keys the transmitter.

Run:   python3 fldigi_net_scheduler.py [--config PATH]
Needs: pip install PySide6   (sched_core.py must be in the same folder)
"""
from __future__ import annotations

import argparse
import html
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QByteArray, QObject, QRegularExpression, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QFontDatabase, QKeySequence, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QSplitter, QStyledItemDelegate, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import sched_core as core
from sched_core import DAYS, NO_CHANGE, Entry

APP_NAME = "FLDigi Net Scheduler"
APP_ID = "FLDigiNetScheduler"

(COL_ON, COL_NAME, COL_DAY, COL_TIME, COL_LOCAL,
 COL_FREQ, COL_MODE, COL_DIGI, COL_SWEET) = range(9)
HEADERS = ["On", "Net / label", "Day (UTC)", "Start UTC", "Local start",
           "Freq (kHz)", "Rig mode", "Digital mode", "Sweet spot (Hz)"]
HEADER_TIPS = {
    COL_ON: "Unchecked rows are kept but never run by the scheduler",
    COL_DAY: "Day of the week in UTC (a 01:00 UTC Tuesday net is Monday evening in the Americas)",
    COL_TIME: "24-hour UTC, HH:MM (0100 and 1:00 are accepted)",
    COL_LOCAL: "Next start converted to this computer's time zone (read-only)",
    COL_FREQ: "Dial (VFO) frequency in kHz, e.g. 7072 or 14070.5. Blank = don't change",
    COL_MODE: "Rig mode sent with rig.set_mode",
    COL_DIGI: "fldigi modem sent with modem.set_by_name",
    COL_SWEET: ("Audio frequency the net's signal uses above (USB) or below (LSB) the listed dial. "
                "The dial is shifted so that signal lands on the centre (1500 Hz). "
                "Example: 7110 kHz USB, sweet spot 900 -> signal at 7110.900, dial 7109.400, carrier 1500"),
}
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

    def execute(self, host, port, entry, label, base, switch_to, rpc_to):
        self.pool.submit(self._run, core.execute_entry, host, port, entry, label, base,
                         switch_to, rpc_to, self.b.log.emit)

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

        self.bridge = Bridge()
        self.bridge.log.connect(self.log)
        self.bridge.status.connect(self._on_status)
        self.bridge.lists.connect(self._on_lists)
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
        self.poll_timer.timeout.connect(lambda: self._poll())
        self.poll_timer.start(max(2, self.settings.poll_interval_s) * 1000)

        self._on_clock()
        self._connect()
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
        self.host_edit.setMaximumWidth(180)
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
        self.base_spin.setToolTip("Audio centre you want every net to land on (fldigi carrier)")
        self.base_spin.valueChanged.connect(self._on_widget_changed)
        self.status_lbl = QLabel("● not connected")
        self.status_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.status_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for w in (QLabel("Host"), self.host_edit, QLabel("Port"), self.port_spin, self.connect_btn,
                  QLabel("   Centre"), self.base_spin):
            cl.addWidget(w)
        cl.addSpacing(12)
        cl.addWidget(self.status_lbl, 1)

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
            "Apply the selected row(s) to fldigi immediately (receive only)")
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
        for col, w in ((COL_ON, 36), (COL_DAY, 100), (COL_TIME, 80), (COL_LOCAL, 120),
                       (COL_FREQ, 100), (COL_MODE, 100), (COL_DIGI, 140), (COL_SWEET, 100)):
            self.table.setColumnWidth(col, w)
        self.table.itemChanged.connect(self._on_item_changed)

        self.next_lbl = QLabel()
        self.next_lbl.setFont(fixed)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))

        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addLayout(tb)
        tl.addWidget(self.table, 1)
        tl.addWidget(self.next_lbl)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(top)
        split.addWidget(self.log_view)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)

        central = QWidget()
        v = QVBoxLayout(central)
        v.addWidget(conn_box)
        v.addWidget(clock_box)
        v.addWidget(split, 1)
        self.setCentralWidget(central)
        self.resize(1250, 820)

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
        on = QTableWidgetItem()
        on.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled |
                    Qt.ItemFlag.ItemIsSelectable)
        on.setCheckState(Qt.CheckState.Checked)
        t.setItem(r, COL_ON, on)
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

    def _get_row(self, r: int) -> Entry:
        t = self.table
        return Entry(
            enabled=t.item(r, COL_ON).checkState() == Qt.CheckState.Checked,
            name=t.item(r, COL_NAME).text().strip(),
            day=t.cellWidget(r, COL_DAY).currentText(),
            start_utc=t.item(r, COL_TIME).text().strip(),
            freq_khz=t.item(r, COL_FREQ).text().strip(),
            rig_mode=t.cellWidget(r, COL_MODE).currentText(),
            digital_mode=t.cellWidget(r, COL_DIGI).currentText(),
            sweet_spot_hz=t.cellWidget(r, COL_SWEET).value(),
        )

    def _set_row(self, r: int, e: Entry):
        t = self.table
        was = self._loading
        self._loading = True
        try:
            t.item(r, COL_ON).setCheckState(Qt.CheckState.Checked if e.enabled else Qt.CheckState.Unchecked)
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

    def _add_row(self):
        r = self.table.rowCount()
        self._loading = True
        self.table.insertRow(r)
        self._init_row(r)
        self._loading = False
        self.table.scrollToBottom()
        self._mark_dirty()

    def _insert_row(self):
        rows = self._selected_rows()
        r = rows[0] if rows else 0
        self._loading = True
        self.table.insertRow(r)
        self._init_row(r)
        self._loading = False
        self._refresh_derived()
        self._mark_dirty()

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
        finally:
            self._loading = False

    def _tuning_text(self, e: Entry) -> str:
        khz = core.parse_freq_khz(e.freq_khz)
        sb = core.rig_sideband(e.rig_mode)
        guess = ""
        if sb is None:
            sb, guess = "USB", " (sideband taken from fldigi at run time; USB shown)"
        dial, car, why = core.plan_tuning(None if khz is None else khz * 1000, e.sweet_spot_hz,
                                          self.base_spin.value(), sb)
        d = f"dial {dial / 1000:.3f} kHz, " if dial is not None else ""
        return f"{d}carrier {car} Hz - {why}{guess}"

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

    def _update_next_label(self, now_u: datetime):
        if not self.sched_chk.isChecked():
            self.next_lbl.setText("Scheduler paused - nothing will run automatically.")
            return
        best = None
        for i, e in enumerate(self.entries()):
            if not e.is_schedulable():
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
            f"→ {e.rig_mode} {e.digital_mode}, {self._tuning_text(e).split(' - ')[0]}")

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
                    due = core.due_entries(entries, minute)
                    if len(due) > 1:
                        self.log("warn", f"{len(due)} entries due at {minute:%a %H:%M} UTC; "
                                         f"running in grid order, the last one wins")
                    for i, e in due:
                        self.log("info", f"Scheduled {minute:%a %H:%M} UTC: {e.label(i)}")
                        self._execute(i, e)
        finally:
            self._arm_minute_timer()

    def _execute(self, row: int, e: Entry):
        s = self.settings
        self.worker.execute(self.host_edit.text().strip(), self.port_spin.value(), e,
                            e.label(row), self.base_spin.value(),
                            s.mode_switch_timeout_s, s.xmlrpc_timeout_s)

    def _run_selected(self):
        rows = [r for r in self._selected_rows() if not self._get_row(r).is_blank()]
        if not rows:
            self.log("warn", "Select a non-empty row to run")
            return
        for r in rows:
            e = self._get_row(r)
            if e.problems():
                self.log("error", f"{e.label(r)}: " + "; ".join(e.problems()))
                continue
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
    def _document(self) -> core.Document:
        s = self.settings
        s.host = self.host_edit.text().strip() or core.DEFAULT_HOST
        s.port = self.port_spin.value()
        s.sweet_spot_base_hz = self.base_spin.value()
        s.scheduler_enabled = self.sched_chk.isChecked()
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
        finally:
            self._loading = False
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
