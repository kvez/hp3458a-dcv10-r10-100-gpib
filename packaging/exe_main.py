"""Entry point of the stand-alone Windows exe (PyInstaller, scripts/build_exe.py).

Double-click start (no arguments): the wizard opens in live mode with the `lab.local.toml`
next to the exe; without it the exe stops with a message (operator decision: simulation only
on request, `--simulate`). Results go to `data/` next to the exe.
Explicit arguments work as with `python scripts/gui.py`. A windowed exe has no console,
so an argument/config error is shown in a message box instead of being lost.
"""

import io
import sys
from contextlib import redirect_stderr
from pathlib import Path


NO_CONFIG = ("Nincs lab.local.toml az exe mellett:\n{path}\n\nÉlő méréshez másold ide a "
             "konfigurációt. Szimuláció csak kérésre: HP3458A-diag.exe --simulate")


def default_argv(argv: list[str], base: Path) -> list[str] | None:
    """None: no mode given and no lab.local.toml next to the exe (never simulate silently)."""
    args = list(argv)
    if not any(a in ("--simulate", "--config") for a in args):
        local = base / "lab.local.toml"
        if not local.exists():
            return None
        args += ["--config", str(local)]
    if "--output" not in args:
        args += ["--output", str(base / "data")]
    return args


def main() -> int:
    from hp3458diag.gui.app import main as gui_main
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    argv = default_argv(sys.argv[1:], base)
    if argv is None:
        from PySide6.QtWidgets import QApplication, QMessageBox
        QApplication.instance() or QApplication(sys.argv[:1])
        QMessageBox.critical(None, "HP 3458A zajdiagnosztika",
                             NO_CONFIG.format(path=base / "lab.local.toml"))
        return 2
    captured = io.StringIO()
    try:
        with redirect_stderr(captured):
            return gui_main(argv)
    except SystemExit as exc:
        if exc.code not in (0, None):
            from PySide6.QtWidgets import QApplication, QMessageBox
            QApplication.instance() or QApplication(sys.argv[:1])
            QMessageBox.critical(None, "HP 3458A zajdiagnosztika",
                                 captured.getvalue().strip() or f"Kilépési kód: {exc.code}")
        return exc.code if isinstance(exc.code, int) else 1


if __name__ == "__main__":
    raise SystemExit(main())
