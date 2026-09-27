"""Schema-validated lab configuration loader. Loading never opens hardware."""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .instrument import DOCUMENTED_IDENTITY
from .transport.visa import VisaSettings
from .validation import MANUAL_P92, ReadingProfile, get_profile

SCHEMA_VERSION = 1


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LabConfig:
    source: str
    visa: VisaSettings | None  # None: no resource chosen; live connection impossible
    accepted_identities: tuple[str, ...]
    document: dict[str, Any]
    reading_profile: ReadingProfile = MANUAL_P92
    stat_crosscheck: str = "absolute_relative"


def _integer(table: dict[str, Any], key: str, default: int) -> int:
    value = table.get(key, default)
    if type(value) is not int:
        raise ConfigError(f"[instrument].{key} must be an integer")
    return value


def parse_config(document: dict[str, Any], source: str = "<memory>") -> LabConfig:
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(f"schema_version must be {SCHEMA_VERSION}")
    instrument = document.get("instrument")
    if not isinstance(instrument, dict):
        raise ConfigError("Missing [instrument] table")
    resource = instrument.get("resource", "")
    backend = instrument.get("visa_backend", "")
    if not isinstance(resource, str) or not isinstance(backend, str):
        raise ConfigError("[instrument].resource and visa_backend must be text")
    accepted = instrument.get("accepted_identities", [DOCUMENTED_IDENTITY])
    if (not isinstance(accepted, list) or not accepted
            or not all(isinstance(text, str) and text.strip() == text and text
                       for text in accepted)):
        raise ConfigError("[instrument].accepted_identities must be non-empty trimmed text")
    limits = (_integer(instrument, "io_timeout_ms", 5000),
              _integer(instrument, "read_chunk_bytes", 4096),
              _integer(instrument, "max_response_bytes", 65536))
    termination = instrument.get("read_termination", "lf")
    if not isinstance(termination, str):
        raise ConfigError("[instrument].read_termination must be text")
    visa = None
    try:
        # Placeholder resource validates the limits even when no resource is chosen.
        settings = VisaSettings(resource.strip() or "GPIB0::0::INSTR", backend.strip(), *limits,
                                termination)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if resource.strip():
        visa = settings
    validation = document.get("validation", {})
    if not isinstance(validation, dict):
        raise ConfigError("[validation] must be a table")
    try:
        profile = get_profile(validation.get("reading_profile", MANUAL_P92.name))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    stat_mode = validation.get("stat_crosscheck", "absolute_relative")
    if stat_mode not in ("absolute_relative", "dmm_half_quantum"):
        raise ConfigError("[validation].stat_crosscheck must be absolute_relative "
                          "or dmm_half_quantum")
    return LabConfig(source, visa, tuple(accepted), document, profile, stat_mode)


def load_config(path: Path) -> LabConfig:
    try:
        with Path(path).open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    return parse_config(document, str(path))
