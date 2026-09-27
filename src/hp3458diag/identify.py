"""Operator-started connection smoke check: open, ID?/REV?/ERRSTR?/ERR?, close.

Sends no trigger, memory, calibration or measurement command. Optional, explicit
H01 steps: a logged Selected Device Clear before the first query (framing recovery
after an earlier timeout, p. 304) and, only after a verified identity, `END ON`
with `END?`/`ERR?` readback (EOI characterization, p. 176). Every raw byte and
backend event is saved to a new, never-overwritten folder.
"""

import base64
from decimal import Decimal
import json
import re
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from . import __version__
from .config import LabConfig
from .instrument import (ErrorRegister, IdentityError, Preflight, ResponseFormatError,
                         preflight_identity, read_error_register, software_environment)
from .persistence import _git_revision, _write_new
from .transport.visa import VisaSettings, VisaTransport


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, re.Pattern):
        return value.pattern
    if callable(value):
        return getattr(value, "__qualname__", repr(value))
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode(), "repr": repr(value)}
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def run_identify(config: LabConfig, output: Path,
                 transport_factory: Callable[[VisaSettings], Any] = VisaTransport, *,
                 device_clear_reason: str | None = None, set_end_on: bool = False
                 ) -> tuple[str, Preflight | None, Path]:
    if config.visa is None:
        raise ValueError("No [instrument].resource in the config; nothing is opened")
    transport = transport_factory(config.visa)
    status, detail, preflight = "TRANSPORT_ERROR", None, None
    end_mode_before_raw: bytes | None = None
    end_mode_raw: bytes | None = None
    end_errors: ErrorRegister | None = None
    try:
        transport.open()
        if device_clear_reason is not None:
            transport.clear_device(device_clear_reason)
        preflight = preflight_identity(transport, config.accepted_identities)
        complete = (preflight.preexisting_errors.complete
                    and preflight.error_register_after is not None
                    and preflight.error_register_after.error is None)
        status = "IDENTIFIED" if complete else "IDENTIFIED_ERROR_LOG_INCOMPLETE"
        if set_end_on and complete:
            end_mode_before_raw = transport.query_raw("END?")
            transport.write("END ON")
            end_mode_raw = transport.query_raw("END?")
            end_errors = read_error_register(transport)
    except IdentityError as exc:
        status, detail = "IDENTITY_REJECTED", str(exc)
    except ResponseFormatError as exc:
        status, detail = "RESPONSE_INVALID", str(exc)
    except (OSError, RuntimeError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            transport.close()
        except OSError as exc:
            detail = f"{detail or ''} close: {exc}".strip()
    session_id = str(uuid.uuid4())
    output.mkdir(parents=True, exist_ok=True)
    folder = output / f"identify-{session_id}"
    folder.mkdir(exist_ok=False)
    payload = {
        "schema_version": 1, "kind": "connection_identify", "session_uuid": session_id,
        "created_utc": datetime.now(timezone.utc).isoformat(), "simulation": False,
        "hardware_validation": "OPERATOR_STARTED_SMOKE_CHECK_ONLY",
        "status": status, "detail": detail, "config_source": config.source,
        "visa_settings": config.visa, "backend_info": getattr(transport, "backend_info", {}),
        "software_version": __version__, "git_revision": _git_revision(),
        "software_environment": software_environment(), "preflight": preflight,
        "device_clear_reason": device_clear_reason, "set_end_on": set_end_on,
        "end_mode_before_raw": end_mode_before_raw, "end_mode_raw": end_mode_raw,
        "error_register_after_end_on": end_errors,
        "trace": list(getattr(transport, "trace", [])),
    }
    _write_new(folder / "identify.json", json.dumps(_jsonable(payload), ensure_ascii=False,
                                                    allow_nan=False, indent=2))
    return status, preflight, folder
