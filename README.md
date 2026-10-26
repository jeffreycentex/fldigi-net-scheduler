# FLDigi Net Scheduler

A cross-platform (Windows, macOS, Linux) desktop app that retunes
[fldigi](http://www.w1hkj.org/) for scheduled digital nets. You keep a weekly
schedule of nets in a grid. At each net's start time (UTC), the app uses
fldigi's XML-RPC interface to set the rig mode, dial frequency, modem and
audio carrier. It can also connect a KiwiSDR and record the net through the
KiwiRemote (or Icom client) remote control API, and end both when the net's
duration is up.

**The app never transmits.** It only changes receive-side settings. Keying
the radio is up to you.

## Features

- Connects to fldigi's XML-RPC server (default `127.0.0.1:7362`). Host and
  port are editable, and a live status line shows the dial, rig mode, modem
  and carrier.
- Local and UTC clocks, plus a countdown to the next scheduled net.
- Schedule grid that starts with 2 rows and shows about 5 before scrolling.
  Use Add row or Insert row for more. Each row has: on/off, label, day,
  start time (UTC), local start time, frequency, rig mode, digital mode,
  sweet spot, SDR, Record and Duration.
- Day, rig mode and digital mode are dropdowns. Once connected, the mode
  lists are loaded from fldigi itself (`rig.get_modes`, `modem.get_names`).
- Sweet spot handling: each net's dial is shifted by the difference between
  its sweet spot and fldigi's PSK sweet spot (details below).
- A built-in scheduler checks every UTC minute. If the app checks a few
  minutes late it catches up, but it does not replay a long gap after the
  computer sleeps.
- The schedule and settings autosave to a JSON file. File › Open and Save As
  let you use other files.
- **SDR:** each net can connect to a KiwiSDR server, with an optional
  password, through the client API. Choose the saved Default SDR, a custom
  server, or Off.
- **Recording:** each net can record to WAV through the client API. The file
  is named after the net, without spaces, plus the UTC date and time.
- **Duration:** when it runs out, the recording stops and an SDR the net
  connected is disconnected. A net still running when the next one starts is
  ended first.
- "Run selected now" starts the selected net immediately, including the SDR,
  recording and duration. "Stop active net" ends the running net early.
- Every step is verified against fldigi and logged (OK, warnings, errors).

## Requirements

- Python 3.9 or newer
- [PySide6](https://pypi.org/project/PySide6/) 6.5 or newer
- fldigi with XML-RPC enabled. It is on by default, port 7362. Tested
  against fldigi 4.2.13.
- For rig mode and frequency changes, fldigi needs working rig control
  (flrig, Hamlib or RigCAT). With KiwiRemote, that is its rigctld on port 4532.
- For SDR and recording: KiwiRemote, or the Icom client, with its remote
  control API turned on (default `127.0.0.1:7373`). Both use the same API on
  the same port. Only one should run at a time, because they share the audio
  and rigctl.

## Install and run

```bash
git clone https://github.com/jeffreycentex/fldigi-net-scheduler.git
cd fldigi-net-scheduler
python3 -m pip install -r requirements.txt
python3 fldigi_net_scheduler.py
```

On Windows, use `py` instead of `python3`. To use a specific schedule file,
add `--config PATH`.

## The schedule grid

| Column | Meaning |
|---|---|
| On | `On` / `Off` dropdown, new rows start `Off`; Off rows are kept but never run |
| Net / label | Free text, shown in the log and the "Next" line |
| Day (UTC) | `Daily`, `Weekdays`, `Weekends`, or `Mon`–`Sun`. Evaluated in **UTC** |
| Start UTC | `HH:MM`, 24-hour. `0100` and `1:00` are also accepted |
| Local start | Read-only: the next start time in this computer's time zone |
| Freq (kHz) | The net's listed dial frequency, e.g. `7110` or `14070.5`. Blank means don't change it |
| Rig mode | Sent with `rig.set_mode`. `(no change)` skips this step |
| Digital mode | fldigi modem name, sent with `modem.set_by_name`. `(no change)` skips this step |
| Sweet spot (Hz) | The net's sweet spot (default 1500, meaning no shift) |
| SDR | Button that opens a dialog: **Off**, **Use the Default SDR**, or **Custom** server and password. Use Off with the Icom client |
| Record | `On` / `Off` (default Off): record the net through the client API |
| Duration | Minutes the net runs (default 60). Required when SDR or Record is used |

The day is evaluated in UTC because the start time is in UTC. For example, a
Tuesday 01:00 UTC net is Monday evening in the Americas. The Local start
column shows this.

## How a scheduled entry is applied

0. **Previous net:** if a net is still running, its recording is stopped. Its
   SDR is disconnected too, unless the new net connects an SDR of its own
   (a new connect replaces the old link).
1. **SDR** (if the row uses one): `POST /connect` with the server and
   password, waiting up to 15 s. The session limit is set to the duration
   plus 2 minutes, so the client's 45-minute limit for connections without a
   password doesn't cut a longer net short. If the server is busy (503) or
   slow to answer (504), the client keeps retrying and the net carries on. If
   the connect fails outright (502), the net is not recorded.
2. **Client tune** (if the client API is running): `POST /tune` with the same
   dial frequency fldigi gets (sweet spot shift included) and the rig mode.
   USB, LSB and AM are sent as they are; data modes such as USB-D or DATA-L
   are sent as USB or LSB. Modes with no equivalent, such as CW or FM, send
   the frequency only. The digital mode is only ever set in fldigi.
3. **Rig mode** (`rig.set_mode`) goes before the frequency, because many
   radios move the VFO when the mode changes. fldigi still sends the mode and
   frequency through rigctl, so the same schedule works direct to a radio.
4. **Dial frequency** (`main.set_frequency`), reduced by
   (FLDigi PSK Sweet Spot − row sweet spot) as described below.
5. **Modem** (`modem.set_by_name`). The app then polls `modem.get_name` to
   confirm the switch worked; some modes, such as OFDM, fail silently.
6. **Carrier** (`modem.set_carrier`). This is always set, even if the modem
   was already active, because fldigi does not re-centre when the mode
   doesn't change.
7. **Recording** (if Record is On): `POST /record/start` with the name
   `<NetNameWithoutSpaces>_{date}T{time}`, for example
   `TexasDigitalNet_20261027T010000Z.wav` (date and time in UTC), and
   `max_seconds` = the duration plus 2 minutes. The client then stops the
   recording itself even if this app is closed.

**At the end of the duration:** `POST /record/stop`, then `POST /disconnect`
if the net connected an SDR. The running net and its end time are saved in
the schedule file, so a restart doesn't lose them. If the end time passed
while the app was closed, the net is ended as soon as the app starts.

### Sweet spot math

The **FLDigi PSK Sweet Spot** field (top of the window, default 1500 Hz) is
where fldigi's carrier is put. Each row's dial frequency is reduced by the
difference between that and the row's sweet spot:

```
dial sent to fldigi = freq - (FLDigi PSK Sweet Spot - row sweet spot)
carrier             = FLDigi PSK Sweet Spot
```

| Freq | Row sweet spot | FLDigi PSK Sweet Spot | Dial sent | Carrier |
|---|---|---|---|---|
| 7110 kHz | 900 Hz | 1500 Hz | 7109.400 kHz | 1500 Hz |
| 7110 kHz | 1500 Hz | 1500 Hz | 7110.000 kHz | 1500 Hz |
| 7110 kHz | 2000 Hz | 1500 Hz | 7110.500 kHz | 1500 Hz |

The same rule applies to every rig mode. If the frequency is blank, the dial
is left alone and the carrier is set to the row's sweet spot. Hover over a
Sweet spot cell to see the computed dial.

### SDR settings

- **Default SDR…** (in the client API box) stores one server and password.
  Rows set to *Use the Default SDR* follow it, so changing it updates all of
  those rows.
- A row set to *Custom* uses its own server and password.
- Passwords are stored in plain text in the schedule file, and are never
  written to the log.
- A row is marked invalid (red cell) if it needs an SDR that isn't set up, or
  if it uses SDR or Record without a duration.

## Where the schedule is saved

The JSON file is autosaved on every edit. Its location depends on the
platform:

| OS | Path |
|---|---|
| macOS | `~/Library/Preferences/FLDigiNetScheduler/schedule.json` |
| Linux | `~/.config/FLDigiNetScheduler/schedule.json` |
| Windows | `%LOCALAPPDATA%\FLDigiNetScheduler\schedule.json` |

The exact path is also shown in the status bar.

Example file:

```json
{
  "app": "FLDigi Net Scheduler",
  "schema_version": 3,
  "settings": { "host": "127.0.0.1", "port": 7362, "sweet_spot_base_hz": 1500,
                "scheduler_enabled": true, "poll_interval_s": 5,
                "mode_switch_timeout_s": 4.0, "xmlrpc_timeout_s": 3.0,
                "api_host": "127.0.0.1", "api_port": 7373, "api_token": "",
                "default_sdr_server": "kiwi.example.net:8073",
                "default_sdr_password": "", "active_net": {} },
  "entries": [
    { "enabled": true, "name": "Example 40m net", "day": "Tue", "start_utc": "01:00",
      "freq_khz": "7110", "rig_mode": "USB", "digital_mode": "THOR22",
      "sweet_spot_hz": 900, "sdr_mode": "default", "sdr_server": "",
      "sdr_password": "", "record": true, "duration_min": 90 }
  ]
}
```

Older files are converted automatically when loaded. Schema 1 stored
`sweet_spot_offset_hz`; rows from schema 2 get SDR Off, Record Off and a
60-minute duration.

## Testing without a radio

`mock_fldigi.py` is a small fake fldigi XML-RPC server. It mimics the
behaviour seen on fldigi 4.2.13: mode switches are asynchronous, setting the
current mode again does nothing, and OFDM modes fail to switch.

`mock_kiwiremote.py` fakes the client API. The servers `busy.example`,
`fail.example` and `slow.example` return 503, 502 and 504. It also accepts
`/tune`.

```bash
python3 mock_fldigi.py          # listens on 127.0.0.1:7362
python3 mock_kiwiremote.py      # listens on 127.0.0.1:7373
python3 fldigi_net_scheduler.py # in another terminal
```

## Project layout

| File | Purpose |
|---|---|
| `fldigi_net_scheduler.py` | PySide6 GUI: grid, clocks, minute timer, background XML-RPC worker |
| `sched_core.py` | Qt-free logic: data model, validation, UTC time math, JSON, fldigi actions, client API, net start/end |
| `mock_fldigi.py` | Fake fldigi server for testing |
| `mock_kiwiremote.py` | Fake KiwiRemote / Icom client API for testing |

## Notes and limits

- fldigi has no XML-RPC call for reading or writing its own sweet spot
  settings, or for RX audio capture.
- Whether `rig.set_mode` reaches the radio depends on fldigi's rig control
  setup. With no rig control configured, it only changes fldigi's display,
  and the log will warn you.
- `modem.get_bandwidth` returns 0 for most modems, so the app doesn't
  use it.

## License

Copyright (C) 2026 jeffreycentex

This program is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option)
any later version. See [LICENSE](LICENSE).
