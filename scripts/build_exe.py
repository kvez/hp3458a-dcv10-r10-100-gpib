"""Build the stand-alone Windows exe of the wizard with PyInstaller (one file, windowed).

Usage: python scripts/build_exe.py      -> dist/HP3458A-diag.exe (build/, dist/ unversioned)

Bundles Python, PySide6, pyqtgraph, numpy, PyVISA and config/lab.example.toml. The VISA
driver (NI-VISA / Keysight IO Libraries, and the GPIB adapter driver) cannot be bundled:
the target PC needs it for live mode; simulation works without it.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--windowed", "--name", "HP3458A-diag",
        "--paths", str(ROOT / "src"),
        "--add-data", f"{ROOT / 'config' / 'lab.example.toml'};config",
        "--collect-submodules", "hp3458diag",
        "--collect-submodules", "pyvisa",      # backends are imported dynamically
        "--collect-submodules", "pyqtgraph",   # Qt bindings are chosen at run time
        "--copy-metadata", "pyvisa",           # software_environment() reports versions
        "--copy-metadata", "PySide6",
        "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        str(ROOT / "packaging" / "exe_main.py"),
    ]
    return subprocess.run(command, cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
