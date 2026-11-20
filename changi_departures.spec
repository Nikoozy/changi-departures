# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build recipe, shared by every platform. You don't run this directly: it is
# used by build_mac.command, build_exe.bat and the GitHub build (.github/workflows/build.yml).
# Produces:  Mac      dist/Changi Departures.app
#            Windows  dist/ChangiDepartures/ChangiDepartures.exe
import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

root = SPECPATH
datas = [(os.path.join(root, "web"), "web")]
binaries, hiddenimports = [], []
for pkg in ("airportsdata", "docx", "selenium", "tzdata"):
    datas += collect_data_files(pkg)
hiddenimports += collect_submodules("selenium")
for pkg in ("reportlab", "rumps"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass  # rumps is Mac-only; without it the app runs without the menu-bar icon

a = Analysis([os.path.join(root, "app.py")], pathex=[root], datas=datas, binaries=binaries,
             hiddenimports=hiddenimports, excludes=["tkinter", "pytest"])
# Selenium ships its driver helper for every OS (and PyInstaller's own Selenium hook adds
# them all). Drop the other platforms' copies: foreign program files inside a Mac app can
# make the code-signing step of the build fail.
_family = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")


def _foreign_helper(dest):
    d = dest.replace("\\", "/")
    if "selenium/webdriver/common/" not in d:
        return False
    sub = d.split("selenium/webdriver/common/", 1)[1].split("/")[0]
    fam = sub.split("-")[0]
    return "/" in d.split("selenium/webdriver/common/", 1)[1] and fam in ("macos", "windows", "linux") and fam != _family


a.datas = [e for e in a.datas if not _foreign_helper(e[0])]
a.binaries = [e for e in a.binaries if not _foreign_helper(e[0])]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ChangiDepartures",
          # Windows keeps its console window (it shows the app is running; closing it quits).
          # The Mac app has none: it lives in the menu bar and quits when its page closes.
          console=sys.platform != "darwin", argv_emulation=False, target_arch=None,
          codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, name="ChangiDepartures")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Changi Departures.app",
        icon=None,
        bundle_identifier="local.changi-departures",
        version="1.0",
        info_plist={
            "CFBundleDisplayName": "Changi Departures",
            "LSUIElement": True,            # menu-bar app: no Dock icon, no window
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
