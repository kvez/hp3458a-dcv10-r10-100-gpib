"""Pure state reducer for the future GUI/worker contract; it performs no I/O."""

from enum import Enum


class State(str, Enum):
    IDLE = "IDLE"
    WAIT_CONNECTION = "WAIT_CONNECTION"
    CONFIGURING = "CONFIGURING"
    SETTLING = "SETTLING"
    ACQUIRING = "ACQUIRING"
    READING = "READING"
    INVALID = "INVALID"
    REVIEW = "REVIEW"
    PAUSED = "PAUSED"
    ABORTING = "ABORTING"
    ABORTED = "ABORTED"
    COMPLETE = "COMPLETE"


_TRANSITIONS = {
    (State.IDLE, "start"): State.WAIT_CONNECTION,
    (State.WAIT_CONNECTION, "operator_confirmed"): State.CONFIGURING,
    (State.CONFIGURING, "configuration_verified"): State.SETTLING,
    (State.SETTLING, "settled"): State.ACQUIRING,
    (State.ACQUIRING, "acquisition_complete"): State.READING,
    (State.READING, "validation_failed"): State.INVALID,
    (State.READING, "validated_and_saved"): State.REVIEW,
    (State.INVALID, "retry_same_memory"): State.READING,
    (State.INVALID, "discard_saved_block_and_remeasure"): State.WAIT_CONNECTION,
    (State.REVIEW, "next"): State.WAIT_CONNECTION,
    (State.REVIEW, "finish"): State.COMPLETE,
    (State.REVIEW, "pause"): State.PAUSED,
    (State.PAUSED, "resume"): State.WAIT_CONNECTION,
    (State.ABORTING, "hold_confirmed_and_evidence_saved"): State.ABORTED,
}


def transition(state: State, event: str) -> State:
    if event == "abort" and state not in (State.IDLE, State.COMPLETE, State.ABORTED):
        return State.ABORTING
    try:
        return _TRANSITIONS[state, event]
    except KeyError as exc:
        raise ValueError(f"Illegal transition: {state.value} -> {event}") from exc
