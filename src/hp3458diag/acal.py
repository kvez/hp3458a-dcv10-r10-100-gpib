"""Separate, operator-confirmed autocal (WP-08). Never part of a measurement plan.

Factory evidence (Ag_3458A_UserGuide_en.pdf): `ACAL [type][,security_code]` (p. 157);
DCV first, then OHMS (pp. 49, 157); at least 2 h powered in a thermally stable environment
and input signals disconnected (pp. 49, 157); do not cycle power or reset while it runs,
otherwise ACAL REQUIRED (p. 49); DCV about 1 min, OHMS about 10 min (p. 158). After the
autocal the relays need time to stabilize: DCV 15 min, OHM 30 min (User's Guide
9018-01343, p. 77) - used here as the project settling policy (C17).

The command is buffered (INBUF ON, pp. 186-187) and completion is observed by serial-poll
READY (p. 306) with a deadline of three times the factory duration; no query is sent while
the routine runs. A missed deadline leaves the bus state unknown: FAULT, nothing further
is sent (recovery is an explicit operator action). A secured autocal is never unlocked:
no security code is sent (the error is recorded). Every raw reply is kept.
"""

import base64
from dataclasses import dataclass, field
from typing import Any, Callable
from .instrument import identify, read_error_register, read_error_strings

ACAL_TYPES = ("DCV", "OHMS")
FACTORY_SECONDS = {"DCV": 60.0, "OHMS": 600.0}           # p. 158
SETTLE_AFTER_S = {"DCV": 900.0, "OHMS": 1800.0}          # 9018-01343 p. 77; policy C17
DEADLINE_FACTOR = 3.0
# Only for the operator's progress display, never for a decision: durations measured live
# on 2026-09-25 (session-96071915, REV 9,1); the factory values stay the deadline basis.
TYPICAL_SECONDS = {"DCV": 165.0, "OHMS": 664.0}
READY = 16                                                # serial poll bit 4 (p. 306)
CONDITIONS = (
    "A műszer legalább 2 órája bekapcsolva, termikusan stabil környezetben (157. o.; a "
    "gyári specifikációhoz 4 óra, 299. o.)",
    "Minden bemeneti jel és DUT leválasztva (49., 157. o.)",
    "ACAL közben nem szabad kikapcsolni vagy resetelni: ACAL REQUIRED lenne (49. o.)",
    "Időtartam: DCV kb. 1 perc, OHMS kb. 10 perc (158. o.); utána projekt-settling DCV 15, "
    "OHMS 30 perc (9018-01343, 77. o.)",
    "Az ACAL megváltoztatja a műszer belső autokalibrációs állandóit (folyamatos memória)",
)


@dataclass(frozen=True)
class AcalRequest:
    types: tuple[str, ...]
    operator_confirmation: str       # the operator's own statement, stored verbatim
    inputs_disconnected: bool
    warmed_up_2h: bool

    def validate(self) -> None:
        if not self.types or any(t not in ACAL_TYPES for t in self.types) or \
                len(set(self.types)) != len(self.types):
            raise ValueError(f"ACAL types must be a subset of {ACAL_TYPES}")
        if list(self.types) != sorted(self.types, key=ACAL_TYPES.index):
            raise ValueError("DCV autocal must precede OHMS (p. 157)")
        if not self.operator_confirmation.strip():
            raise ValueError("ACAL needs the operator's own confirmation text")
        if not (self.inputs_disconnected and self.warmed_up_2h):
            raise ValueError("ACAL conditions not confirmed (inputs disconnected, >= 2 h on)")


@dataclass
class AcalStep:
    kind: str
    command: str
    started_s: float
    duration_s: float | None = None
    polls: int = 0
    last_status: int | None = None
    error_register: dict[str, Any] | None = None
    error_strings: list[tuple[int, str]] = field(default_factory=list)
    state: str = "RUNNING"           # COMPLETE / ERROR / TIMEOUT


@dataclass
class AcalResult:
    status: str                      # COMPLETE / ERROR / FAULT / REFUSED
    request: AcalRequest
    identity: str | None = None
    temp_before_raw: str | None = None
    temp_after_raw: str | None = None
    errors_before: list[tuple[int, str]] = field(default_factory=list)
    steps: list[AcalStep] = field(default_factory=list)
    settle_s: float = 0.0
    note: str = ""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def run_acal(transport: Any, request: AcalRequest, *, accepted_identities: tuple[str, ...],
             clock: Callable[[], float], sleep: Callable[[float], None],
             event_sink: Callable[[dict[str, Any]], None] | None = None,
             poll_interval_s: float = 1.0,
             progress: Callable[[dict[str, Any]], None] | None = None) -> AcalResult:
    """Run the requested autocals in factory order. The caller guarantees that no block
    owns the reading memory (no pending block) and re-establishes the measurement baseline
    afterwards (engine.baseline with readback): the autocal's effect on settings is not
    assumed."""
    request.validate()

    def event(name: str, **data: Any) -> None:
        if event_sink is not None:
            event_sink({"event": name, "monotonic_s": clock(), **data})

    result = AcalResult("REFUSED", request)
    if getattr(transport, "framing_unknown", False):
        result.note = "bus framing unknown: recover first"
        return result
    event("acal_confirmed", types=list(request.types),
          operator_confirmation=request.operator_confirmation)
    result.identity, raw = identify(transport, accepted_identities)
    event("acal_identity", identity=result.identity, raw=_b64(raw))
    log = read_error_strings(transport)
    result.errors_before = list(log.entries)
    register = read_error_register(transport)
    event("acal_errors_before", entries=log.entries, raw=[_b64(r) for r in log.raw],
          register=register.value, register_raw=_b64(register.raw))
    raw = transport.query_raw("TEMP?")
    result.temp_before_raw = raw.decode("ascii", "replace").strip()
    event("acal_temp_before", raw=_b64(raw))
    transport.write("TARM HOLD")
    transport.write("INBUF ON")
    status = "COMPLETE"
    for number, kind in enumerate(request.types, 1):
        command = f"ACAL {kind}"
        step = AcalStep(kind, command, clock())
        result.steps.append(step)
        event("acal_started", routine=kind, command=command)
        transport.write(command, acal=True)
        deadline = step.started_s + DEADLINE_FACTOR * FACTORY_SECONDS[kind]
        while True:
            sleep(poll_interval_s)
            step.polls += 1
            step.last_status = transport.serial_poll()
            if progress is not None:
                progress({"routine": kind, "number": number, "count": len(request.types),
                          "elapsed_s": clock() - step.started_s,
                          "typical_s": TYPICAL_SECONDS[kind],
                          "factory_s": FACTORY_SECONDS[kind],
                          "deadline_s": DEADLINE_FACTOR * FACTORY_SECONDS[kind],
                          "types": list(request.types)})
            if step.last_status & READY:
                break
            if clock() > deadline:
                step.state = "TIMEOUT"
                break
        step.duration_s = clock() - step.started_s
        if step.state == "TIMEOUT":
            event("acal_timeout", routine=kind, duration_s=step.duration_s,
                  last_status=step.last_status)
            result.status = "FAULT"
            result.note = (f"ACAL {kind}: READY not seen in {step.duration_s:.0f} s; nothing "
                           "further sent. Do not power-cycle; recover explicitly.")
            return result
        # ERRSTR? first: each reply clears one bit, ERR? clears them all (pp. 177-178), so
        # reading ERR? first would lose the messages. ERR? then shows any residue.
        log = read_error_strings(transport)
        step.error_strings = list(log.entries)
        register = read_error_register(transport)
        step.error_register = {"value": register.value, "bits": list(register.bits),
                               "raw": _b64(register.raw), "error": register.error,
                               "errstr_raw": [_b64(r) for r in log.raw],
                               "errstr_complete": log.complete}
        # an unparsable or incomplete reply is not a success either
        if step.error_strings or not log.complete or register.value != 0:
            step.state, status = "ERROR", "ERROR"
        else:
            step.state = "COMPLETE"
        event("acal_finished", routine=kind, state=step.state, duration_s=step.duration_s,
              polls=step.polls, error_register=step.error_register,
              error_strings=step.error_strings)
        if step.state == "ERROR":
            break  # never continue with OHMS after a failed DCV
    transport.write("INBUF OFF")
    raw = transport.query_raw("TEMP?")
    result.temp_after_raw = raw.decode("ascii", "replace").strip()
    event("acal_temp_after", raw=_b64(raw))
    done = [s.kind for s in result.steps if s.state in ("COMPLETE", "ERROR")]  # ran
    result.settle_s = max((SETTLE_AFTER_S[k] for k in done), default=0.0)
    result.status = status
    event("acal_result", status=status, settle_s=result.settle_s,
          temp_before=result.temp_before_raw, temp_after=result.temp_after_raw)
    return result


def run_acal_session(config: Any, output: Any, request: AcalRequest,
                     transport_factory: Callable[..., Any] | None = None,
                     clock: Callable[[], float] | None = None,
                     sleep: Callable[[float], None] | None = None) -> tuple[AcalResult, Any]:
    """CLI entry: its own durable session (kind acal_session), raw bus journal, the result
    record; the session never measures. Returns (result, folder)."""
    import time
    from dataclasses import replace
    from .session_store import SessionStore
    request.validate()
    if config.visa is None:
        raise ValueError("No [instrument].resource in the config; nothing is opened")
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    store = SessionStore.create(output, {
        "kind": "acal_session", "simulation": False,
        "hardware_validation": "OPERATOR_STARTED_ACAL", "acal_types": list(request.types),
        "operator_acal_confirmation": request.operator_confirmation,
        "visa_resource": config.visa.resource}, monotonic=clock)
    if transport_factory is None:
        from .transport.visa import VisaTransport
        transport_factory = VisaTransport
    transport = transport_factory(replace(config.visa, timeout_ms=10_000,
                                          read_termination="lf"),
                                  journal=store.bus_journal_path)
    try:
        transport.open()
        result = run_acal(transport, request, accepted_identities=config.accepted_identities,
                          clock=clock, sleep=sleep, event_sink=store.event_sink)
        store.save_record("acal.json", {"acal": result})
        if result.status == "FAULT":
            store.journal.append("session_left_open", reason=result.note)
        else:
            store.close(f"ACAL_{result.status}")
    finally:
        if not getattr(transport, "framing_unknown", False):
            try:
                transport.close()
            except OSError:
                pass
    return result, store.folder

