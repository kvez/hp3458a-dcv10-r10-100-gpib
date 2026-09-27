import argparse
import os
import sys
from math import isfinite
from pathlib import Path
from .config import ConfigError, load_config
from .domain import default_plan
from .memory_reader import validate_memory
from .persistence import save_simulation
from .simulator import FakeMemory, encode_readings, synthetic_values


def main(argv: list[str] | None = None) -> int:
    code = _main(argv)
    from .transport.visa import process_poisoned
    if process_poisoned():
        # PyVISA's atexit viClose hangs after a native VISA crash (H04 evidence).
        print("VISA library crashed in this process; exiting without native cleanup.")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    return code


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HP3458A offline diagnostic foundation")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list-plan", action="store_true")
    mode.add_argument("--simulate", action="store_true")
    mode.add_argument("--simulate-session", action="store_true",
                      help="SIMULATION: full engine session on a virtual-clock instrument")
    parser.add_argument("--session-tests", default="A-NPLC1,C-NPLC1,E-R10-NPLC100",
                        help="--simulate-session: comma-separated plan test IDs")
    parser.add_argument("--session-fault", default="none",
                        help="--simulate-session: simulated instrument fault")
    mode.add_argument("--identify", action="store_true",
                      help="LIVE: open the configured resource, ID?/REV?/ERRSTR?/ERR? only")
    mode.add_argument("--rmem-frame-check", action="store_true",
                      help="LIVE H01: DCV 10 block, RMEM with LF and EOI framing")
    mode.add_argument("--characterize", action="store_true",
                      help="LIVE H05/C21: DCV 0.1/10 and OHMF 10/100 blocks, STAT vs PC")
    mode.add_argument("--engine-check", action="store_true",
                      help="LIVE WP-02: engine preflight/probes/DCV+OHMF blocks (OHMF current)")
    mode.add_argument("--acal", metavar="TYPES",
                      help="separate operator-confirmed autocal (WP-08): DCV, OHMS or DCV,OHMS")
    mode.add_argument("--live-session", action="store_true",
                      help="LIVE: run --session-tests through the engine into a durable session")
    mode.add_argument("--recover-session", type=Path, metavar="FOLDER",
                      help="LIVE: explicit recovery of an interrupted session (never measures)")
    mode.add_argument("--list-interrupted", action="store_true",
                      help="list OPEN sessions under --output (files only)")
    mode.add_argument("--export-session", type=Path, metavar="FOLDER",
                      help="export a stored session to CSV/JSON/Markdown (files only)")
    parser.add_argument("--confirm-wiring", help="--live-session: operator wiring statement")
    parser.add_argument("--confirm-acal", help="--acal: the operator's own statement")
    parser.add_argument("--inputs-disconnected", action="store_true",
                        help="--acal: all input signals and DUTs are disconnected (pp. 49, 157)")
    parser.add_argument("--warmed-up-2h", action="store_true",
                        help="--acal: powered >= 2 h in a thermally stable place (p. 157)")
    parser.add_argument("--settle-s", type=float, help="--live-session: settling override (s)")
    parser.add_argument("--discard-memory-reason",
                        help="--live-session: discard pre-existing memory that cannot be archived")
    parser.add_argument("--recovery-reason", help="--recover-session: operator reason")
    parser.add_argument("--read-memory", action="store_true",
                        help="--recover-session: read an unsaved block as a RETRY read")
    parser.add_argument("--safe-end", action="store_true",
                        help="--recover-session: afterwards TARM HOLD + DCV 10 (no ohms current)")
    mode.add_argument("--acceptance", action="store_true",
                      help="LIVE H03/H04/H02-abort acceptance (OHMF current into the DUT)")
    parser.add_argument("--dut-id", help="--characterize: DUT on the input (OHMF current!)")
    parser.add_argument("--dut-nominal-ohm", type=float,
                        help="--characterize: nominal (not calibrated) DUT value")
    parser.add_argument("--blocks", help="--characterize: comma-separated block labels")
    parser.add_argument("--samples", type=int, default=100, help="--rmem-frame-check N")
    parser.add_argument("--config", type=Path, help="lab TOML; required for --identify")
    parser.add_argument("--device-clear", metavar="REASON",
                        help="--identify: logged SDC before the first query (p. 304)")
    parser.add_argument("--set-end-on", action="store_true",
                        help="--identify: after verified identity send END ON, read END?/ERR?")
    parser.add_argument("--include-optional", action="store_true")
    parser.add_argument("--test-id", default="E-R10-NPLC100")
    parser.add_argument("--fault", choices=("none", "majority", "invalid", "timeout"), default="none")
    parser.add_argument("--drift-per-sample", type=float, default=0.0)
    parser.add_argument("--output", type=Path, default=Path("data"))
    args = parser.parse_args(argv)
    if args.simulate_session:
        return _simulate_session(parser, args)
    if args.list_interrupted:
        return _list_interrupted(args)
    if args.export_session:
        from .export import export_session
        print(f"EXPORT: {export_session(args.export_session)}")
        return 0
    if args.live_session:
        return _live_session(parser, args)
    if args.acal:
        return _acal(parser, args)
    if args.recover_session:
        return _recover_session(parser, args)
    if args.identify:
        return _identify(parser, args)
    if args.rmem_frame_check:
        return _frame_check(parser, args)
    if args.characterize:
        return _characterize(parser, args)
    if args.acceptance:
        return _acceptance(parser, args)
    if args.engine_check:
        return _engine_check(parser, args)
    if args.device_clear is not None or args.set_end_on:
        parser.error("--device-clear and --set-end-on require --identify")
    plan = default_plan(args.include_optional)
    if args.list_plan:
        for point in plan:
            print(f"{point.test_id:24} {point.mode:4} {point.range_value:6g} {point.unit:3} "
                  f"NPLC={point.nplc:3} N={point.n:3} DUT={point.dut_id}")
        return 0
    points = {point.test_id: point for point in plan}
    if args.test_id not in points:
        parser.error("Unknown test ID; use --list-plan")
    if not isfinite(args.drift_per_sample):
        parser.error("Drift must be finite")
    point = points[args.test_id]
    # Synthetic amplitude is a demo parameter, not a 3458A specification.
    sigma = 6.4e-6 if point.mode == "OHMF" else 1e-7
    values = synthetic_values(point.n, point.nominal, sigma, args.drift_per_sample)
    raw = encode_readings(tuple(reversed(values)))
    corrupted = b"+9.99917120E+00" + raw[15:]
    replies = {"none": [], "majority": [corrupted, None, None],
               "invalid": [raw[:-3] + b"\r\n", b"NaN\r\n", b"\r\n"],
               "timeout": [TimeoutError("Injected incomplete bus response")]}
    fake = FakeMemory(values, replies[args.fault])
    # Deliberately explicit SIMULATION tolerance, not a hardware acceptance tolerance.
    result = validate_memory(fake, point, absolute_tolerance=1e-12, relative_tolerance=1e-8)
    folder = save_simulation(args.output, point, result, fake.commands,
                             f"fault={args.fault}; drift_per_sample={args.drift_per_sample}")
    print(f"SIMULATION: {result.status}; RMEM={result.memory_reads.status}; saved={folder}")
    return 0 if result.status == "VALIDATED" else 2


def _identify(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.config is None:
        parser.error("--identify requires an explicit --config file")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(str(exc))
    if config.visa is None:
        parser.error("[instrument].resource is empty; no instrument is opened")
    from .identify import run_identify
    if args.device_clear is not None and not args.device_clear.strip():
        parser.error("--device-clear requires a non-empty reason")
    status, preflight, folder = run_identify(config, args.output,
                                             device_clear_reason=args.device_clear,
                                             set_end_on=args.set_end_on)
    print(f"LIVE IDENTIFY: {status}; resource={config.visa.resource}; saved={folder}")
    if preflight is not None:
        print(f"ID={preflight.identity!r}; REV={','.join(preflight.revision)}; "
              f"pre-existing errors={list(preflight.preexisting_errors.entries)}")
    return 0 if status == "IDENTIFIED" else 3


def _frame_check(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.config is None:
        parser.error("--rmem-frame-check requires an explicit --config file")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(str(exc))
    if config.visa is None:
        parser.error("[instrument].resource is empty; no instrument is opened")
    if not 2 <= args.samples <= 100:
        parser.error("--samples must be 2..100")
    from .frame_check import run_frame_check
    status, folder = run_frame_check(config, args.output, args.samples)
    print(f"LIVE RMEM FRAME CHECK: {status}; resource={config.visa.resource}; saved={folder}")
    return 0 if status in ("EOI_PRESENT", "EOI_TIMEOUT") else 3


def _characterize(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.config is None or not args.dut_id or args.dut_nominal_ohm is None:
        parser.error("--characterize requires --config, --dut-id and --dut-nominal-ohm")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(str(exc))
    if config.visa is None:
        parser.error("[instrument].resource is empty; no instrument is opened")
    from .characterize import DEFAULT_BLOCKS, NPLC_BLOCKS, run_characterization
    blocks = DEFAULT_BLOCKS
    if args.blocks:
        known = {spec.label: spec for spec in DEFAULT_BLOCKS + NPLC_BLOCKS}
        labels = [label.strip() for label in args.blocks.split(",")]
        if not labels or any(label not in known for label in labels):
            parser.error(f"--blocks must name labels from {list(known)}")
        blocks = tuple(known[label] for label in labels)
    try:
        status, folder = run_characterization(config, args.output, args.dut_id,
                                              args.dut_nominal_ohm, blocks)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"LIVE CHARACTERIZATION: {status}; resource={config.visa.resource}; saved={folder}")
    return 0 if status == "COMPLETED" else 3


def _acceptance(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.config is None or not args.dut_id or args.dut_nominal_ohm is None:
        parser.error("--acceptance requires --config, --dut-id and --dut-nominal-ohm")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(str(exc))
    if config.visa is None:
        parser.error("[instrument].resource is empty; no instrument is opened")
    from .acceptance import run_acceptance
    try:
        status, folder = run_acceptance(config, args.output, args.dut_id, args.dut_nominal_ohm)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"LIVE ACCEPTANCE H03/H04/H02: {status}; resource={config.visa.resource}; "
          f"saved={folder}")
    return 0 if status == "PASS" else 3


def _simulate_session(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    from .session_sim import run_simulated_session
    from .simulator import SIMULATED_FAULTS
    if args.session_fault not in SIMULATED_FAULTS:
        parser.error(f"--session-fault must be one of {SIMULATED_FAULTS}")
    plan = {point.test_id: point for point in default_plan(True)}
    from .domain import canonical_test_id
    ids = [canonical_test_id(item.strip()) for item in args.session_tests.split(",")
           if item.strip()]
    if not ids or any(item not in plan for item in ids):
        parser.error("Unknown test ID in --session-tests; use --list-plan")
    status, folder, summary = run_simulated_session([plan[i] for i in ids], args.output,
                                                    args.session_fault)
    for test_id, state, result in summary:
        print(f"SIMULATION BLOCK {test_id}: {state} (validation: {result})")
    print(f"SIMULATION SESSION: {status}; saved={folder}")
    return 0 if status == "COMPLETED" and all(s == "VALIDATED" for _, s, _ in summary) else 2


def _engine_check(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.config is None or not args.dut_id or args.dut_nominal_ohm is None:
        parser.error("--engine-check requires --config, --dut-id and --dut-nominal-ohm")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(str(exc))
    if config.visa is None:
        parser.error("[instrument].resource is empty; no instrument is opened")
    from .engine_check import run_engine_check
    try:
        status, folder = run_engine_check(config, args.output, args.dut_id,
                                          args.dut_nominal_ohm)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"LIVE ENGINE CHECK: {status}; resource={config.visa.resource}; saved={folder}")
    return 0 if status == "PASS" else 3


def _load_live_config(parser: argparse.ArgumentParser, args: argparse.Namespace):
    if args.config is None:
        parser.error("this mode requires an explicit --config file")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(str(exc))
    if config.visa is None:
        parser.error("[instrument].resource is empty; no instrument is opened")
    return config


def _list_interrupted(args: argparse.Namespace) -> int:
    from .session_store import find_interrupted
    found = find_interrupted(args.output)
    for item in found:
        pending = item.pending_block
        print(f"OPEN {item.folder}: last={item.last_event}; pending="
              f"{pending and pending['test_id']}; saved={bool(item.pending_saved)}; "
              f"truncated_tail={item.truncated_tail}; chain_ok={item.chain_ok}")
    print(f"{len(found)} interrupted session(s)")
    return 0


def _live_session(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    config = _load_live_config(parser, args)
    if not args.confirm_wiring:
        parser.error("--live-session requires --confirm-wiring with the operator's statement")
    plan = {point.test_id: point for point in default_plan(True)}
    ids = [item.strip() for item in args.session_tests.split(",") if item.strip()]
    if not ids or any(item not in plan for item in ids):
        parser.error("Unknown test ID in --session-tests; use --list-plan")
    from .domain import series_end
    points = [plan[i] for i in ids]
    if series_end(points, 0) != len(points):
        parser.error("One --confirm-wiring covers one wiring series (same DUT and function); "
                     "use the GUI wizard (scripts/gui.py) for DUT or wiring changes")
    from .live_session import run_live_session
    status, folder, summary = run_live_session(
        config, args.output, points, args.confirm_wiring, args.settle_s,
        args.discard_memory_reason)
    for test_id, state, result in summary:
        print(f"LIVE BLOCK {test_id}: {state} (validation: {result})")
    print(f"LIVE SESSION: {status}; saved={folder}")
    return 0 if status == "COMPLETED" and all(s == "VALIDATED" for _, s, _ in summary) else 3


def _acal(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    from .acal import CONDITIONS, AcalRequest, run_acal_session
    config = _load_live_config(parser, args)
    request = AcalRequest(tuple(t.strip().upper() for t in args.acal.split(",") if t.strip()),
                          args.confirm_acal or "", args.inputs_disconnected, args.warmed_up_2h)
    try:
        request.validate()
    except ValueError as exc:
        parser.error(f"--acal: {exc}. Conditions: " + " | ".join(CONDITIONS))
    result, folder = run_acal_session(config, args.output, request)
    for step in result.steps:
        print(f"ACAL {step.kind}: {step.state} in {step.duration_s:.0f} s ({step.polls} polls)")
    print(f"ACAL: {result.status}; TEMP? {result.temp_before_raw} -> {result.temp_after_raw}; "
          f"settle {result.settle_s:.0f} s before measuring; saved={folder}")
    if result.note:
        print(f"NOTE: {result.note}")
    return 0 if result.status == "COMPLETE" else 3


def _recover_session(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    config = _load_live_config(parser, args)
    if not args.recovery_reason:
        parser.error("--recover-session requires --recovery-reason")
    import uuid
    from dataclasses import replace
    from .recovery import recover_session, safe_end
    from .transport.visa import VisaTransport
    import json
    from .recovery import recoverable
    meta = json.loads((args.recover_session / "session.json").read_text(encoding="utf-8"))
    if not recoverable(meta):  # before any file or bus access: a closed session stays as is
        parser.error(f"--recover-session: not an interrupted session "
                     f"({meta.get('lifecycle')} {meta.get('status')})")
    transport = VisaTransport(replace(config.visa, timeout_ms=10_000, read_termination="lf"),
                              journal=args.recover_session / f"bus-recovery-{uuid.uuid4()}.jsonl")
    transport.open()
    try:
        report = recover_session(args.recover_session, transport, profile=config.reading_profile,
                                 stat_crosscheck=config.stat_crosscheck,
                                 accepted_identities=config.accepted_identities,
                                 reason=args.recovery_reason, read_memory=args.read_memory)
        if args.safe_end:
            ended = safe_end(transport)
            print(f"SAFE END: {ended}")
    finally:
        try:
            transport.close()
        except OSError:
            pass
    print(f"RECOVERY: {report['decision']}; session={args.recover_session}")
    return 0
