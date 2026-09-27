"""Pure wizard state for the PySide6 GUI (WP-05). No Qt, no I/O.

The GUI enables exactly the actions returned by `allowed_actions`; the worker reports
facts as events and `reduce` is the only way the phase changes. Operator gates are
explicit: every wiring series (consecutive points on the same DUT, function and optional
flag, see `series_end`) needs one wiring confirmation, RETRY is only offered for an INVALID
block, re-measurement is a separate choice, an abort is "ABORTING" until the worker
confirms it, and FAULT allows nothing but an explicit recovery, export or a new session.
"""

from dataclasses import dataclass, field, replace
from enum import Enum
from .domain import series_end  # noqa: F401  (re-export for the GUI)


class Phase(str, Enum):
    IDLE = "IDLE"                    # no session
    CONNECTING = "CONNECTING"
    READY = "READY"                  # session open, preflight + baseline done
    WAIT_WIRING = "WAIT_WIRING"      # operator must confirm the wiring for this block
    SETTLING = "SETTLING"
    ACQUIRING = "ACQUIRING"
    READING = "READING"
    REVIEW = "REVIEW"                # block saved and released
    INVALID = "INVALID"              # block saved, memory still owned: RETRY possible
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    ABORTING = "ABORTING"
    ABORTED = "ABORTED"
    FAULT = "FAULT"                  # bus/HOLD state unknown: explicit recovery only
    STORAGE_FAULT = "STORAGE_FAULT"  # evidence not durable: stop, recover later
    COMPLETE = "COMPLETE"
    ACAL = "ACAL"                    # separate operator-confirmed autocal (WP-08) running


BUSY = {Phase.CONNECTING, Phase.SETTLING, Phase.ACQUIRING, Phase.READING,
        Phase.PAUSE_REQUESTED, Phase.ABORTING, Phase.ACAL}

ACTIONS = ("new_session", "start", "confirm_wiring", "pause", "abort", "retry",
           "remeasure", "skip_optional", "recover", "export", "acal")


@dataclass(frozen=True)
class WizardState:
    phase: Phase = Phase.IDLE
    point_count: int = 0
    point_index: int = 0          # index of the current / next point
    current_optional: bool = False
    has_session: bool = False
    pause_pending: bool = False   # pause requested while busy: honoured after the block
    series_end: int = 0           # exclusive end of the wiring series being measured
    resume_phase: Phase | None = None  # where a finished ACAL returns to
    message: str = ""
    log: tuple[str, ...] = field(default_factory=tuple)

    @property
    def busy(self) -> bool:
        return self.phase in BUSY


def allowed_actions(state: WizardState) -> set[str]:
    phase = state.phase
    allowed: set[str] = set()
    if phase in (Phase.IDLE, Phase.COMPLETE, Phase.ABORTED, Phase.FAULT,
                 Phase.STORAGE_FAULT):
        allowed.add("new_session")
    if phase in (Phase.READY, Phase.REVIEW, Phase.PAUSED) and \
            state.point_index < state.point_count:
        allowed.add("start")
    if phase == Phase.WAIT_WIRING:
        allowed |= {"confirm_wiring", "abort"}
        if state.current_optional:
            allowed.add("skip_optional")
    if phase in (Phase.SETTLING, Phase.ACQUIRING, Phase.READING):
        allowed |= {"pause", "abort"}
    if phase == Phase.PAUSE_REQUESTED:
        allowed.add("abort")
    if phase == Phase.INVALID:
        allowed |= {"retry", "remeasure"}
    if phase == Phase.REVIEW and state.point_index > 0:
        allowed.add("remeasure")  # separate, operator-chosen new block of the last point
    if phase == Phase.FAULT:
        allowed.add("recover")
    if state.has_session and not state.busy:
        allowed.add("export")
    if state.has_session and phase in (Phase.READY, Phase.REVIEW, Phase.COMPLETE):
        allowed.add("acal")  # no block owns the memory; never offered inside a plan step
    return allowed


class IllegalAction(ValueError):
    pass


def require(state: WizardState, action: str) -> None:
    if action not in allowed_actions(state):
        raise IllegalAction(f"{action} not allowed in {state.phase.value}")


def reduce(state: WizardState, event: str, /, **data) -> WizardState:
    """Apply one worker fact or operator action; returns the new state."""
    def log(text: str) -> tuple[str, ...]:
        return state.log + (text,)

    if event == "new_session":
        require(state, "new_session")
        return WizardState(Phase.CONNECTING, message="Kapcsolódás…", log=log("Új munkamenet"))
    if event == "session_ready":
        return replace(state, phase=Phase.READY, has_session=True,
                       point_count=data["point_count"], point_index=0,
                       message="Munkamenet kész", log=log("Preflight és baseline rendben"))
    if event == "session_failed":
        return replace(state, phase=Phase.FAULT if data.get("fault") else Phase.IDLE,
                       message=data["reason"], log=log(f"Hiba: {data['reason']}"))
    if event == "start":
        require(state, "start")
        if state.phase == Phase.PAUSED and state.point_index < state.series_end:
            # same DUT and wiring: the series continues without a new gate
            return replace(state, phase=Phase.SETTLING, pause_pending=False,
                           message="Sorozat folytatása szünet után",
                           log=log("Folytatás: a sorozat következő blokkja (azonos bekötés)"))
        return replace(state, phase=Phase.WAIT_WIRING,
                       current_optional=data.get("optional", False), pause_pending=False,
                       series_end=data.get("series_end", state.point_index + 1),
                       message="Bekötés ellenőrzése", log=log("Bekötési kapu"))
    if event == "confirm_wiring":
        require(state, "confirm_wiring")
        return replace(state, phase=Phase.SETTLING, message="Stabilizálás",
                       log=log("Bekötés megerősítve"))
    if event == "skip_optional":
        require(state, "skip_optional")
        nxt = max(state.series_end, state.point_index + 1)  # the whole optional series
        skipped = nxt - state.point_index
        return replace(state, phase=Phase.COMPLETE if nxt >= state.point_count else
                       Phase.REVIEW, point_index=nxt,
                       log=log(f"Opcionális sorozat kihagyva ({skipped} pont)"))
    if event == "acquiring":
        if state.phase not in (Phase.SETTLING, Phase.PAUSE_REQUESTED, Phase.ABORTING):
            return state
        return replace(state, phase=Phase.ACQUIRING if state.phase == Phase.SETTLING
                       else state.phase, message="Mérés folyamatban")
    if event == "pause":
        require(state, "pause")
        return replace(state, phase=Phase.PAUSE_REQUESTED, pause_pending=True,
                       message="Szünet kérve: a futó blokk befejeződik és mentődik, utána megáll",
                       log=log("Szünet kérve (a futó blokk végigmegy)"))
    if event == "abort":
        require(state, "abort")
        if state.phase == Phase.WAIT_WIRING:  # nothing runs on the instrument
            return replace(state, phase=Phase.ABORTED, message="Megszakítva",
                           log=log("Megszakítva a bekötési kapunál"))
        return replace(state, phase=Phase.ABORTING, message="Megszakítás folyamatban",
                       log=log("Megszakítás kérve"))
    if event == "block_done":
        outcome_state = data["state"]
        if outcome_state == "FAULT":
            return replace(state, phase=Phase.FAULT, message="Buszállapot ismeretlen",
                           log=log("FAULT: helyreállítás szükséges"))
        if outcome_state == "ABORTED" or state.phase == Phase.ABORTING:
            return replace(state, phase=Phase.ABORTED, message="Megszakítva",
                           log=log(f"Megszakítva ({outcome_state})"))
        if outcome_state == "INVALID":
            return replace(state, phase=Phase.INVALID,
                           message="A tárolt mintákat nem sikerült egyezően kiolvasni. "
                                   "Hagyd változatlanul a bekötést. Ugyanazt a memóriát "
                                   "újraolvassuk?", log=log("INVALID blokk (mentve)"))
        nxt = state.point_index + 1
        if nxt >= state.point_count:
            phase = Phase.COMPLETE  # a pause after the last block has nothing to wait for
        elif state.pause_pending:
            phase = Phase.PAUSED
        elif outcome_state == "VALIDATED" and nxt < min(state.series_end, state.point_count):
            return replace(state, phase=Phase.SETTLING, point_index=nxt,
                           message=f"Blokk kész: {outcome_state}; a sorozat folytatódik",
                           log=log(f"Blokk kész: {outcome_state} → sorozat következő blokkja"))
        else:
            phase = Phase.REVIEW
        message = (f"Szünetel — a blokk kész és mentve ({outcome_state}). "
                   "Folytatás: a következő pont bekötési kapuja" if phase == Phase.PAUSED
                   else f"Blokk kész: {outcome_state}")
        return replace(state, phase=phase, point_index=nxt, pause_pending=False,
                       message=message,
                       log=log(f"Blokk kész: {outcome_state}"))
    if event == "retry":
        require(state, "retry")
        return replace(state, phase=Phase.READING, message="Ugyanaz a memória újraolvasása",
                       log=log("RETRY (nincs új mérés)"))
    if event == "remeasure":
        require(state, "remeasure")
        index = state.point_index if state.phase == Phase.INVALID else state.point_index - 1
        return replace(state, phase=Phase.WAIT_WIRING, point_index=index,
                       series_end=index + 1,  # a re-measurement is a single block
                       message="Újramérés: bekötés ellenőrzése",
                       log=log("Újramérés külön döntéssel"))
    if event == "acal":
        require(state, "acal")
        return replace(state, phase=Phase.ACAL, resume_phase=state.phase,
                       message="ACAL fut (külön művelet) — ne kapcsold ki a műszert",
                       log=log("ACAL indítva kezelői megerősítéssel"))
    if event == "acal_done":
        status = data.get("status")
        if status == "FAULT":
            return replace(state, phase=Phase.FAULT, message=data.get("note", ""),
                           log=log(f"ACAL FAULT: {data.get('note', '')}"))
        return replace(state, phase=state.resume_phase or Phase.READY,
                       message=f"ACAL: {status}", log=log(f"ACAL kész: {status}"))
    if event == "recover":
        require(state, "recover")
        return replace(state, phase=Phase.READING, message="Helyreállítás",
                       log=log("Helyreállítás indítva"))
    if event == "recovered":
        phase = Phase.INVALID if data.get("memory_intact") else Phase.ABORTED
        if state.phase == Phase.ABORTING:  # Abort pressed meanwhile: stop, never continue
            phase = Phase.ABORTED
        return replace(state, phase=phase, message="Helyreállítva",
                       log=log(f"Helyreállítva ({'memória ép' if data.get('memory_intact') else 'blokk eldobva'})"))
    if event == "storage_fault":
        return replace(state, phase=Phase.STORAGE_FAULT, message=data.get("reason", ""),
                       log=log(f"Mentési hiba: {data.get('reason', '')}"))
    if event == "fault":
        return replace(state, phase=Phase.FAULT, message=data.get("reason", ""),
                       log=log(f"FAULT: {data.get('reason', '')}"))
    raise IllegalAction(f"Unknown event {event}")
