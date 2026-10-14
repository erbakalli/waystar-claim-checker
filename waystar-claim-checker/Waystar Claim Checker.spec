# Build the live application and bundle the local engine.
import os
import sys
import tkinter
import _tkinter
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)
support = root / '.kantime-build'
tcl = support / 'tcl8.6'
tk = support / 'tk8.6'
if not tcl.is_dir() or not tk.is_dir():
    tcl = Path(sys.base_prefix) / 'tcl' / 'tcl8.6'
    tk = Path(sys.base_prefix) / 'tcl' / 'tk8.6'
if not tcl.is_dir() or not tk.is_dir():
    raise SystemExit('Build requires Windows Python with Tcl/Tk, or local .kantime-build/tcl8.6 and tk8.6.')
os.environ['TCL_LIBRARY'] = str(tcl)
os.environ['TK_LIBRARY'] = str(tk)
datas, binaries, hiddenimports = collect_all('playwright')
hiddenimports += ['tkinter', 'tkinter.ttk', 'tkinter.filedialog', 'tkinter.messagebox', '_tkinter']
datas += [(str(tcl), '_tcl_data'), (str(tk), '_tk_data')]
assets = root / 'src' / 'waystar_claim_checker' / 'assets'
datas += [(str(assets), 'waystar_claim_checker/assets')]
a = Analysis(
    [str(root / 'src/waystar_claim_checker/__main__.py')],
    pathex=[str(root / 'src'), str(root.parent / 'Kantime-engine-main/src'),
            str(Path(tkinter.__file__).parent.parent), str(Path(_tkinter.__file__).parent)],
    binaries=binaries, datas=datas, hiddenimports=hiddenimports,
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    noarchive=False, optimize=0,
)
if 'tkinter' not in {name for name, *_ in a.pure}:
    raise SystemExit('Build is missing tkinter; refusing to produce an unusable desktop application.')
if not any(Path(name).stem == '_tkinter' for name, *_ in a.binaries):
    raise SystemExit('Build is missing _tkinter; refusing to produce an unusable desktop application.')
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [], name='Waystar Claim Checker',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    upx_exclude=[], runtime_tmpdir=None, console=False,
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
    icon=str(assets / 'waystar.ico'),
)
