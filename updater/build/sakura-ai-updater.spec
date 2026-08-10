"""PyInstaller onefile specification for the host updater."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


# build.sh executes PyInstaller from the repository root; PyInstaller runs
# spec files with exec(), so __file__ is intentionally unavailable here.
UPDATER_SRC = (Path.cwd() / "updater" / "src").resolve()
ENTRYPOINT = UPDATER_SRC / "sakura_ai_updater" / "__main__.py"

# The updater package uses dynamic backend/IPC imports; collect only its own
# package. Official hooks handle FastAPI/Uvicorn and their runtime dependencies.
hiddenimports = collect_submodules("sakura_ai_updater")
a = Analysis(
    [str(ENTRYPOINT)],
    pathex=[str(UPDATER_SRC)],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

# Keep this as a single onefile EXE. The host contract is one executable
# whose CArchive is unpacked into a controlled TMPDIR at run time.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="sakura-ai-updater",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
