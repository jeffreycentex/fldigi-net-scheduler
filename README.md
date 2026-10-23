# FLDigi Net Scheduler

A cross-platform (Windows, macOS, Linux) desktop app that retunes
[fldigi](http://www.w1hkj.org/) for scheduled digital nets. You keep a weekly
schedule of nets in a grid. At each net's start time (UTC), the app uses
fldigi's XML-RPC interface to set the rig mode, dial frequency, modem and
audio carrier.

**The app never transmits.** It only changes receive-side settings. Keying
the radio is up to you.

## Features

- Connects to fldigi's XML-RPC server (default `127.0.0.1:7362`). Host and
  port are editable, and a live status line shows the dial, rig mode, modem
  and carrier.
- Local and UTC clocks, plus a countdown to the next scheduled net.
- Schedule grid with 30 rows to start and room for more. Each row has:
  on/off, label, day, start time (UTC), local start time, frequency, rig
  mode, digital mode and sweet spot.
- Day, rig mode and digital mode are dropdowns. Once connected, the mode
  lists are loaded from fldigi itself (`rig.get_modes`, `modem.get_names`).
- Sideband-aware sweet spot handling: the dial is shifted so each net's
  signal lands at 1500 Hz audio (details below).
- A built-in scheduler checks every UTC minute. If the app checks a few
  minutes late it catches up, but it does not replay a long gap after the
  computer sleeps.
- The schedule and settings autosave to a JSON file. File › Open and Save As
  let you use other files.
- "Run selected now" applies a row immediately, for testing.
- Every step is verified against fldigi and logged (OK, warnings, errors).

## Requirements

- Python 3.9 or newer
- [PySide6](https://pypi.org/project/PySide6/) 6.5 or newer
- fldigi with XML-RPC enabled. It is on by default, port 7362. Tested
  against fldigi 4.2.13.
- For rig mode and frequency changes, fldigi needs working rig control
  (flrig, Hamlib or RigCAT).

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
| On | Unchecked rows are kept but never run |
| Net / label | Free text, shown in the log and the "Next" line |
| Day (UTC) | `Daily`, `Weekdays`, `Weekends`, or `Mon`–`Sun`. Evaluated in **UTC** |
| Start UTC | `HH:MM`, 24-hour. `0100` and `1:00` are also accepted |
| Local start | Read-only: the next start time in this computer's time zone |
| Freq (kHz) | The net's listed dial frequency, e.g. `7110` or `14070.5`. Blank means don't change it |
| Rig mode | Sent with `rig.set_mode`. `(no change)` skips this step |
| Digital mode | fldigi modem name, sent with `modem.set_by_name`. `(no change)` skips this step |
| Sweet spot (Hz) | The audio frequency the net's signal uses relative to its listed dial (default 1500) |

The day is evaluated in UTC because the start time is in UTC. For example, a
Tuesday 01:00 UTC net is Monday evening in the Americas. The Local start
column shows this.

## How a scheduled entry is applied

1. **Rig mode** (`rig.set_mode`) goes first, because many radios move the
   VFO when the mode changes.
2. **Dial frequency** (`main.set_frequency`), shifted for the sweet spot as
   described below.
3. **Modem** (`modem.set_by_name`). The app then polls `modem.get_name` to
   confirm the switch worked; some modes, such as OFDM, fail silently.
4. **Carrier** (`modem.set_carrier`). This is always set, even if the modem
   was already active, because fldigi does not re-centre when the mode
   doesn't change.

### Sweet spot math

The net's signal sits at the listed dial plus (USB) or minus (LSB) the sweet
spot audio frequency. The app moves the dial so that signal lands on the
centre frequency (1500 Hz by default, set at the top of the window), and
sets fldigi's carrier to that centre.

```
USB:  dial = freq + (sweet - centre)
LSB:  dial = freq - (sweet - centre)
```

| Example | Net's signal (RF) | Dial set to | Carrier |
|---|---|---|---|
| 7110 kHz USB, sweet spot 900 | 7110.900 | 7109.400 | 1500 |
| 7110 kHz LSB, sweet spot 900 | 7109.100 | 7110.600 | 1500 |

For AM, FM, CW and FSK rig modes, or when the frequency is blank, the dial is
left as entered and the carrier is set to the sweet spot. If the rig mode is
`(no change)`, the sideband is read from fldigi (`rig.get_mode`, then
`main.get_wf_sideband`). Hover over a Sweet spot cell to see the computed
dial and carrier.

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
  "schema_version": 2,
  "settings": { "host": "127.0.0.1", "port": 7362, "sweet_spot_base_hz": 1500,
                "scheduler_enabled": true, "poll_interval_s": 5,
                "mode_switch_timeout_s": 4.0, "xmlrpc_timeout_s": 3.0 },
  "entries": [
    { "enabled": true, "name": "Example 40m net", "day": "Tue", "start_utc": "01:00",
      "freq_khz": "7110", "rig_mode": "USB", "digital_mode": "THOR22",
      "sweet_spot_hz": 900 }
  ]
}
```

Files from schema 1, which stored `sweet_spot_offset_hz`, are converted
automatically when loaded.

## Testing without a radio

`mock_fldigi.py` is a small fake fldigi XML-RPC server. It mimics the
behaviour seen on fldigi 4.2.13: mode switches are asynchronous, setting the
current mode again does nothing, and OFDM modes fail to switch.

```bash
python3 mock_fldigi.py          # listens on 127.0.0.1:7362
python3 fldigi_net_scheduler.py # in another terminal
```

## Project layout

| File | Purpose |
|---|---|
| `fldigi_net_scheduler.py` | PySide6 GUI: grid, clocks, minute timer, background XML-RPC worker |
| `sched_core.py` | Qt-free logic: data model, validation, UTC time math, JSON, fldigi actions |
| `mock_fldigi.py` | Fake fldigi server for testing |

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
