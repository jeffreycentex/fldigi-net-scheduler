# CLAUDE.md

Guidance for Claude (and other contributors) working in this repository.

## What this is
FLDigi Net Scheduler: a PySide6 desktop app (Windows/macOS/Linux) that retunes
fldigi over XML-RPC on a weekly UTC schedule. Per entry it sets rig mode, dial
frequency (shifted for the sweet spot), modem and audio carrier. Optionally it
connects an SDR and records the net through the KiwiRemote / Icom client HTTP
API, and ends both when the net's duration runs out. **It must never
transmit**: never call `main.tx`, `main.tune`, `main.run_macro`,
`text.add_tx*`, `wefax.send_file` or `navtex.send_message`.

## Layout
- `sched_core.py`: Qt-free logic. Data model (`Entry`, `Settings`, `Document`),
  validation, UTC time math (`next_occurrence`, `due_entries`, `MinuteTracker`),
  sweet spot math (`plan_tuning`), JSON persistence and the
  fldigi actions (`poll_status`, `fetch_lists`, `execute_entry`), the client
  API (`KiwiApi`, `start_net`, `end_net`, `ActiveNet`, `recording_name`). Put
  all testable logic here.
- `fldigi_net_scheduler.py`: GUI only. All XML-RPC calls go through `Worker`
  (a single background thread). Results come back through `Bridge` Qt signals.
  Never call XML-RPC on the GUI thread.
- `mock_fldigi.py`: fake fldigi server for tests and manual runs.
- `mock_kiwiremote.py`: fake client API. `busy.`, `fail.` and `slow.` servers
  return 503, 502 and 504.

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

## Sweet spot semantics (specified by the user, don't change silently)
`Settings.sweet_spot_base_hz` is shown in the UI as **"FLDigi PSK Sweet Spot"**
(default 1500). `Entry.sweet_spot_hz` is the row's sweet spot. One rule applies
to every rig mode and sideband:
- `dial = freq - (fldigi_psk_sweet_spot - row_sweet_spot)`
- `carrier = fldigi_psk_sweet_spot`
- Blank frequency: dial unchanged, carrier = row sweet spot.

Example: 7110 kHz with sweet spot 900 and FLDigi 1500 gives dial 7109.400 kHz,
carrier 1500. The math is in `plan_tuning()`; keep the GUI tooltip using it too.

## KiwiRemote / Icom client API (spec: KiwiRemote's API.md, v1)
- Base URL `http://<api_host>:<api_port>/api/v1` (default `127.0.0.1:7373`).
  POSTs must send `Content-Type: application/json`, even with `{}`. Use the
  optional token as `Authorization: Bearer`. `KiwiApi` bypasses proxies.
- The Kiwi and Icom clients expose the same API on the same port, and only one
  runs at a time (they share audio and rigctl). Icom nets use SDR Off and only
  record.
- Start order (`start_net`): end the previous active net, then `/connect`
  (`wait` 15, `session_limit_min` = duration + 2), then the fldigi steps, then
  `/record/start` (`name` = net name without spaces + `_{date}T{time}`,
  `max_seconds` = duration*60 + 120). A 502 skips recording. 503/504 carry on.
  A 409 stops the client's existing recording and retries once.
- End (`end_net`): `/record/stop`, then `/disconnect` only if this net
  connected the SDR. If the next net also connects an SDR, don't disconnect in
  between, because a connect replaces the link.
- The active net is persisted in `Settings.active_net` and ended by the GUI's
  250 ms clock check (`_check_active`), so it survives restarts.
- Per-row SDR: `sdr_mode` = `off` | `default` (uses
  `Settings.default_sdr_server/password`) | `custom` (row's own). Passwords are
  stored in plain JSON by the user's choice. Never log them.

## Conventions
- Times are entered in UTC (`HH:MM`). Days are evaluated in UTC.
- The grid starts with `MIN_ROWS` (2) rows and is sized for `VISIBLE_ROWS` (5)
  before scrolling. Blank rows are not saved.
- Bump `SCHEMA_VERSION` when the JSON shape changes, and migrate in
  `Entry.from_dict` / `Settings.from_dict`. Never break existing schedule files.
- Saves are atomic (write `.tmp`, then `os.replace`). A corrupt file is
  renamed to `.bad-<timestamp>` and never overwritten.
- Combo and spin cells ignore the mouse wheel unless focused
  (`NoWheelComboBox` / `NoWheelSpinBox`), so scrolling can't change values.
- Use the `_loading` guard when changing table cells programmatically
  (`setBackground` and `setText` emit `itemChanged`).
- License: GPL-3.0-or-later. Keep the SPDX header on every source file.
