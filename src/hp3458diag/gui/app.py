"""GUI entry point: `python scripts/gui.py --simulate` or `--config config/lab.local.toml`.

Simulation uses the real engine and store with a SimulatedInstrument on an accelerated
virtual clock; the window says SZIMULÁCIÓ in large letters. Live mode needs a config
with a resource; the operator starts every block through the wiring gate.
"""

import argparse
import sys
from pathlib import Path
from ..config import ConfigError, load_config, parse_config
from ..domain import DUT_LABELS, canonical_test_id, default_plan
from .window import PlanRow, WizardWindow
from .worker import SessionWorker, WorkerSettings

# In the stand-alone exe (PyInstaller) the bundled files live under sys._MEIPASS.
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))


def build(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="HP 3458A zajdiagnosztika — varázsló")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--simulate", action="store_true")
    mode.add_argument("--config", type=Path)
    parser.add_argument("--tests", help="comma-separated plan IDs (default: required plan)")
    parser.add_argument("--include-optional", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("data"))
    parser.add_argument("--sim-speed", type=float, default=60.0)
    parser.add_argument("--sim-fault", default="none")
    args = parser.parse_args(argv)
    if args.simulate:
        import tomllib
        document = tomllib.loads((ROOT / "config/lab.example.toml").read_text(encoding="utf-8"))
        document["instrument"]["resource"] = "GPIB0::22::INSTR"  # never opened in simulation
        config = parse_config(document, "simulation")
    else:
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            parser.error(str(exc))
        if config.visa is None:
            parser.error("[instrument].resource is empty; no instrument is opened")
    plan = {p.test_id: p for p in default_plan(True)}
    if args.tests:
        ids = [canonical_test_id(i.strip()) for i in args.tests.split(",") if i.strip()]
        if any(i not in plan for i in ids):
            parser.error("Unknown test ID; use scripts/run.py --list-plan")
        points = [plan[i] for i in ids]
    else:
        points = list(default_plan(args.include_optional))
    rows = [PlanRow(p.test_id, p.dut_id, f"{DUT_LABELS.get(p.dut_id, p.dut_id)}: {p.mode} "
                    f"{p.range_value:g} NPLC {p.nplc} N {p.n}",
                    p.optional, p.mode) for p in points]
    return args, config, points, rows


def main(argv: list[str] | None = None) -> int:
    from PySide6.QtWidgets import QApplication
    args, config, points, rows = build(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    window: WizardWindow | None = None

    def factory() -> SessionWorker:
        meta = {"warm_up_confirmed": window.warmup.isChecked(),
                "last_acal_operator_note": window.last_acal.text() or None,
                "operator_note": window.note.text() or None}
        return SessionWorker(WorkerSettings(config, args.output, points, args.simulate,
                                            args.sim_speed, args.sim_fault, meta,
                                            window.discard_reason, window.discard_count))
    window = WizardWindow(factory, rows, args.simulate)
    window.resize(1200, 900)
    window.show()
    return app.exec()
