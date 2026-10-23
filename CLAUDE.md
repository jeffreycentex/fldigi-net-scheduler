# CLAUDE.md

Guidance for Claude (and other contributors) working in this repository.

## What this is
FLDigi Net Scheduler: a PySide6 desktop app (Windows/macOS/Linux) that retunes
fldigi over XML-RPC on a weekly UTC schedule. Per entry it sets rig mode, dial
frequency (sweet-spot shifted), modem and audio carrier. **It must never
transmit**: never call `main.tx`, `main.tune`, `main.run_macro`,
`text.add_tx*`, `wefax.send_file` or `navtex.send_message`.

## Layout
- `sched_core.py`: Qt-free logic. Data model (`Entry`, `Settings`, `Document`),
  validation, UTC time math (`next_occurrence`, `due_entries`, `MinuteTracker`),
  sweet spot math (`rig_sideband`, `plan_tuning`), JSON persistence and the
  fldigi actions (`poll_status`, `fetch_lists`, `execute_entry`). Put all
  testable logic here.
- `fldigi_net_scheduler.py`: GUI only. All XML-RPC calls go through `Worker`
  (a single background thread). Results come back through `Bridge` Qt signals.
  Never call XML-RPC on the GUI thread.
- `mock_fldigi.py`: fake fldigi server for tests and manual runs.

## Run / test
```bash
python3 -m pip install -r requirements.txt
python3 mock_fldigi.py 17362 &                     # fake fldigi
python3 fldigi_net_scheduler.py --config /tmp/s.json
```
Core logic can be exercised without Qt: import `sched_core` and call
`execute_entry(...)` against the mock. Keep `python3 -m py_compile *.py` clean.
Headless Qt: `QT_QPA_PLATFORM=offscreen`.

## fldigi XML-RPC facts (verified on fldigi 4.2.13, 176 methods)
- `modem.set_by_names` does not exist. Use `modem.set_by_name` (exact,
  case-sensitive names from `modem.get_names`, e.g. `Cont-8/250`, `THOR22`,
  `MFSK16`, `OLIVIA-8/500`).
- Mode switching is asynchronous. Poll `modem.get_name` until it matches.
  Setting the mode that is already active is a no-op and does not re-centre,
  so always call `modem.set_carrier` afterwards.
- With "start at sweet spot" enabled, a real mode change moves the carrier to
  fldigi's own sweet spot (CW, RTTY, or "PSK et al."). Wait ~0.5 s after the
  switch before setting the carrier, or fldigi may overwrite it.
- OFDM500F/OFDM750F/OFDM3500 fail to switch silently. `RSID_EOT` is not a
  modem. Both are filtered by `EXCLUDE_DIGITAL`.
- `main.get_frequency` is deprecated. Read with `rig.get_frequency`
  (`get_dial_hz` falls back to the old call). Set with `main.set_frequency`.
- `modem.get_bandwidth` returns 0 for most modems. Don't rely on it.
- There is no XML-RPC access to fldigi's sweet spot settings or to RX audio
  capture.
- `rig.set_mode` only reaches the radio if fldigi has rig control (flrig,
  Hamlib or RigCAT).

## Sweet spot semantics (agreed with the user, don't change silently)
`Entry.sweet_spot_hz` is the net's audio frequency relative to its listed dial.
The dial is shifted so the signal lands on the centre (`Settings.sweet_spot_base_hz`,
default 1500), and the carrier is set to the centre:
- USB: `dial = freq + (sweet - centre)`
- LSB: `dial = freq - (sweet - centre)`
- AM/FM/CW/FSK, or a blank frequency: dial as entered, carrier = sweet.

Example: 7110 kHz USB with sweet spot 900 gives the signal at 7110.900, dial
7109.400, carrier 1500.

## Conventions
- Times are entered in UTC (`HH:MM`). Days are evaluated in UTC.
- Bump `SCHEMA_VERSION` when the JSON shape changes, and migrate in
  `Entry.from_dict` / `Settings.from_dict`. Never break existing schedule files.
- Saves are atomic (write `.tmp`, then `os.replace`). A corrupt file is
  renamed to `.bad-<timestamp>` and never overwritten.
- Combo and spin cells ignore the mouse wheel unless focused
  (`NoWheelComboBox` / `NoWheelSpinBox`), so scrolling can't change values.
- Use the `_loading` guard when changing table cells programmatically
  (`setBackground` and `setText` emit `itemChanged`).
- License: GPL-3.0-or-later. Keep the SPDX header on every source file.
