# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path
import sys

root = Path(SPECPATH)
analysis = Analysis(
    [str(root / "fldigi_net_scheduler.py")],
    pathex=[str(root)],
    datas=[(str(root / "assets" / "scheduler.png"), "assets")],
)
archive = PYZ(analysis.pure)

exe = EXE(
    archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="fldigi-net-scheduler",
    console=sys.platform != "win32",
    icon=str(root / "assets" / "scheduler.ico") if sys.platform == "win32" else None,
    upx=False,
)
