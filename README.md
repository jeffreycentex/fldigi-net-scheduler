# FLDigi Net Scheduler

Cross-platform (Windows / macOS / Linux) PySide6 app that drives fldigi over
XML-RPC on a weekly UTC schedule. At each scheduled minute it sets, in order:

1. rig mode (`rig.set_mode`)
2. dial frequency (`main.set_frequency`, entered in kHz)
3. fldigi modem (`modem.set_by_name`, verified with `modem.get_name`)
4. audio carrier (`modem.set_carrier`) = centre (1500 Hz)

The dial is shifted so the net's signal (listed dial + sweet spot audio) lands on the centre:
USB `dial = freq + (sweet - 1500)`, LSB `dial = freq - (sweet - 1500)`.
Example: 7110 kHz USB, sweet spot 900 Hz -> signal at 7110.900 RF, dial 7109.400, carrier 1500.
AM/FM/CW/FSK, or a blank frequency: dial as entered, carrier = sweet spot.

It never transmits.

## Install & run
    python3 -m pip install -r requirements.txt
    python3 fldigi_net_scheduler.py            # Windows: py fldigi_net_scheduler.py
Keep `sched_core.py` in the same folder.

Options: `--config PATH` to use a specific schedule file.

## Where the schedule is saved
Autosaved (JSON) on every edit to the per-user app config folder:
- macOS:   ~/Library/Preferences/FLDigiNetScheduler/schedule.json
- Linux:   ~/.config/FLDigiNetScheduler/schedule.json
- Windows: %LOCALAPPDATA%\FLDigiNetScheduler\schedule.json
The exact path is shown in the status bar. File > Save As / Open for other files.

## Grid
| Column | Notes |
|---|---|
| On | unchecked rows never run |
| Day (UTC) | Daily, Weekdays, Weekends, Mon..Sun - evaluated in UTC |
| Start UTC | HH:MM 24 h (0100 and 1:00 accepted) |
| Local start | read-only, next start in this computer's time zone |
| Freq (kHz) | dial frequency; blank = leave unchanged |
| Rig mode / Digital mode | lists come from fldigi once connected; "(no change)" skips the step |
| Sweet spot (Hz) | the net's audio offset from its listed dial (default 1500 = no shift); hover for the computed dial |

## Testing without a radio
    python3 mock_fldigi.py            # fake fldigi on 127.0.0.1:7362
Then start the app, fill a row, and use "Run selected now".
