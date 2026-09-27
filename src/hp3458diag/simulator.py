"""Deterministic reading-memory simulator, not an analog instrument model."""

import random
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from .commands import check_command


# Reply formats per validation profile: manual p. 92 and H01-observed REV 9,1 (C21).
_READING_FORMATS = {"manual_p92": "+.8E", "hp3458a_rev9_1": " .9E"}


def encode_readings(values: tuple[float, ...], profile: str = "manual_p92") -> bytes:
    spec = _READING_FORMATS[profile]
    return (",".join(format(value, spec) for value in values) + "\r\n").encode("ascii")


def synthetic_values(n: int, nominal: float = 10.0, sigma: float = 6.4e-6,
                     drift_per_sample: float = 0.0, seed: int = 3458) -> tuple[float, ...]:
    rng = random.Random(seed)
    return tuple(nominal + rng.gauss(0, sigma) + i * drift_per_sample for i in range(n))


class FakeMemory:
    """Memory is immutable during reads. RMEM returns newest first.

    `replies` injects raw RMEM replies; None means an uncorrupted reply.
    Statistics use independent Decimal arithmetic on the stored ASCII readings.
    """

    def __init__(self, chronological: tuple[float, ...],
                 replies: list[bytes | None | Exception] | None = None,
                 stat_override: dict[str, Decimal] | None = None,
                 rmath_format: str = "plus16") -> None:
        if rmath_format not in ("plus16", "engineering"):
            raise ValueError("rmath_format must be plus16 or engineering")
        self.rmath_format = rmath_format
        self.values = tuple(Decimal(f"{x:+.8E}") for x in chronological)
        self.replies = list(replies or [])
        self.stat_override = stat_override or {}
        self.commands: list[str] = []
        self.math_ready = False
        self.memory_mode = "FIFO"

    def write(self, command: str) -> None:
        check_command(command)
        self.commands.append(command)
        if command == "MMATH STAT":
            self.math_ready = True
        elif command == "MEM OFF":
            self.memory_mode = "OFF"
        elif command != "TARM HOLD":
            raise NotImplementedError("FakeMemory models completed memory only")

    def query_raw(self, command: str) -> bytes:
        check_command(command)
        self.commands.append(command)
        if command.startswith("RMEM 1,"):
            count = int(command.split(",")[1])
            if count != len(self.values):
                raise ValueError("FakeMemory: count does not match stored data")
            self.memory_mode = "OFF"
            if self.replies:
                reply = self.replies.pop(0)
                if isinstance(reply, Exception):
                    raise reply
                if reply is not None:
                    return reply
            return encode_readings(tuple(float(x) for x in reversed(self.values)))
        if command == "MCOUNT?":
            return f"{len(self.values)}\r\n".encode()
        if command.startswith("RMATH "):
            if not self.math_ready:
                raise RuntimeError("MMATH STAT must precede RMATH")
            with localcontext() as context:
                context.prec = 50
                n = len(self.values)
                mean = sum(self.values) / n
                sdev = (sum((x - mean)**2 for x in self.values) / (n - 1)).sqrt()
                stats = {"MEAN": mean, "SDEV": sdev, "LOWER": min(self.values),
                         "UPPER": max(self.values), "NSAMP": Decimal(n)}
                stats.update(self.stat_override)
                # Query precision in this simulator is explicit, not a firmware promise.
                value = stats[command.split()[1]]
                if self.rmath_format == "engineering":  # REV 9,1 reply grammar
                    return (engineering(+value) + "\r\n").encode()
                return f"{value:+.16E}\r\n".encode()
        raise NotImplementedError(f"Not modeled: {command}")

    def serial_poll(self) -> int:
        return 16  # Completed memory is ready; no acquisition timing emulation.

    def close(self) -> None:
        pass


class VirtualClock:
    """Monotonic test clock: `sleep` advances time instantly (engine tests, fake session)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("negative sleep")
        self.now += seconds


# Seconds per reading measured on the real HP3458A REV 9,1 (docs/validation/h05),
# AZERO ON, OCOMP ON for OHMF. Used by the simulator and the engine's READY deadline.
SECONDS_PER_READING = {("DCV", 1): 0.042, ("DCV", 10): 0.41, ("DCV", 100): 4.0,
                       ("OHMF", 1): 2.0, ("OHMF", 10): 2.64, ("OHMF", 100): 27.0}


def engineering(value: Decimal) -> str:
    """Scalar reply as observed on REV 9,1: 9 significant digits, exponent multiple of 3,
    space instead of "+" (e.g. ' 650.716370E-09', C21)."""
    if value == 0:
        return " 0.0000000E+00"  # real REV 9,1 zero: 7 decimals (DELAY? after DELAY 0, WP-02)
    exp3 = (value.adjusted() // 3) * 3
    decimals = 9 - (value.adjusted() - exp3 + 1)
    mantissa = value.scaleb(-exp3).quantize(Decimal(1).scaleb(-decimals),
                                            rounding=ROUND_HALF_EVEN)
    if abs(mantissa) >= 1000:  # rounding rolled over, e.g. 999.9999999 -> 1.00000000E+03
        exp3, decimals = exp3 + 3, 8
        mantissa = value.scaleb(-exp3).quantize(Decimal("1E-8"), rounding=ROUND_HALF_EVEN)
    sign = "" if mantissa < 0 else " "
    return f"{sign}{mantissa:.{decimals}f}E{exp3:+03d}"


SIMULATED_FAULTS = ("none", "slow", "already_done", "stall", "extra", "stale_memory",
                    "rmem_timeout", "corrupt_rmem", "config_error", "drop_on_arm",
                    "no_hold_after", "acal_error", "acal_hang")
# Factory autocal durations (User Guide p. 158); the simulator is busy (READY clear) for this.
ACAL_SECONDS = {"DCV": 60.0, "OHMS": 600.0}


class SimulatedInstrument:
    """Transport-compatible HP 3458A command model on a virtual clock.

    Models what the engine relies on: settings and readback, reading memory (FIFO/CONT/OFF,
    RMEM newest first, MFORMAT clears), INBUF ON with serial-poll READY, timed acquisition
    from SECONDS_PER_READING, MMATH STAT/RMATH (exact, 9 significant digits), the error
    register, and SDC side effects (p. 304): output cleared, triggering stopped, resumed by
    any following command other than TARM HOLD. It is NOT an analog or firmware model.
    A query while a block is still acquiring raises TimeoutError (framing unknown), like an
    input-buffered query that cannot be answered before the block ends.
    """

    def __init__(self, clock: VirtualClock, fault: str = "none", nominal_ohm: float = 10.0,
                 seed: int = 3458) -> None:
        if fault not in SIMULATED_FAULTS:
            raise ValueError(f"Unknown simulated fault {fault!r}")
        self.clock, self.fault, self.nominal_ohm = clock, fault, nominal_ohm
        self.rng = random.Random(seed)
        self.mode, self.range_value = "DCV", 10.0
        self.nplc, self.azero, self.ocomp = 1, True, False
        self.delay: float | None = None
        self.tarm, self.mem, self.inbuf, self.nrdgs = "HOLD", "OFF", False, 1
        self.memory: list[float] = []
        if fault == "stale_memory":
            self.memory = [1.23e-6] * 5  # an older block that MEM FIFO fails to clear
        self.acq: dict | None = None
        self.trigger_disabled = False
        self.err = 0
        self.math = False
        self.rmem_calls = 0
        self.commands: list[str] = []
        self.clears: list[str] = []
        self.framing_unknown = False
        self.temp = 34.0
        self.acal_until: float | None = None

    # --- timing -------------------------------------------------------------------
    def per_reading(self) -> float:
        base = SECONDS_PER_READING.get((self.mode, self.nplc), self.nplc / 50 * 2)
        return base * (10 if self.fault == "slow" else 1)

    def _value(self) -> float:
        if self.mode == "OHMF":
            return self.nominal_ohm + self.rng.gauss(0, 2e-5)
        return self.rng.gauss(1e-6, 5e-7)

    def _resume_after_sdc(self) -> None:
        # SDC side effect: any command except TARM HOLD resumes the triggering (p. 304).
        if self.trigger_disabled and self.acq is not None:
            self.trigger_disabled = False
            stored = len(self.memory) - self.acq["base"]
            self.acq["start"] = self.clock.monotonic() - stored * self.per_reading()

    def _update(self) -> None:
        if self.acq is None or self.trigger_disabled:
            return
        target = self.acq["n"] + (1 if self.fault == "extra" else 0)
        if self.fault == "already_done":
            done = target
        else:
            elapsed = self.clock.monotonic() - self.acq["start"]
            done = max(0, int(elapsed // self.per_reading()))
        if self.fault == "stall":
            done = min(done, 2)
        done = min(done, target)
        while len(self.memory) - self.acq["base"] < done:
            self.memory.append(float(format(self._value(), " .9E")))
        if done >= target:
            self.acq = None
            if self.fault != "no_hold_after":
                self.tarm = "HOLD"

    # --- transport interface -----------------------------------------------------
    def _acal_busy(self) -> bool:
        if self.acal_until is not None and self.clock.monotonic() >= self.acal_until:
            self.acal_until = None
        return self.acal_until is not None

    def write(self, command: str, *, acal: bool = False) -> None:
        check_command(command, acal=acal)
        self.commands.append(command)
        if self._acal_busy():
            self.framing_unknown = True
            raise TimeoutError("simulated: command while autocal is running")
        if acal:  # INBUF ON: buffered, bus released; busy (READY clear) for the routine
            kind = command.split()[1]
            self.acal_until = (float("inf") if self.fault == "acal_hang" else
                               self.clock.monotonic() + ACAL_SECONDS[kind])
            if self.fault == "acal_error":
                self.err |= 2  # bit 1, calibration error (p. 177)
            return
        if command != "TARM HOLD":
            self._resume_after_sdc()
        self._update()
        if self.acq is not None and not self.trigger_disabled and command != "TARM HOLD":
            self.framing_unknown = True
            raise TimeoutError("simulated: command while the block is still acquiring")
        if command == "PRESET NORM":
            self.mode, self.range_value, self.nplc, self.azero = "DCV", 10.0, 1, True
            self.ocomp, self.delay, self.tarm, self.mem = False, None, "AUTO", "OFF"
            self.inbuf, self.math, self.nrdgs = False, False, 1
        elif command == "TARM HOLD":
            self.tarm, self.acq, self.trigger_disabled = "HOLD", None, False
        elif command == "TARM SGL":
            if self.fault == "drop_on_arm":
                self.framing_unknown = True
                raise OSError("simulated bus failure on TARM SGL")
            self.tarm = "SGL"
            if self.mem != "OFF":
                # DELAY acts once per trigger, before the first reading (p. 170; WP-02
                # live: 5 OHMF readings with DELAY 1 took 10.3 s = 5 x ~1.9 s + 1 s).
                self.acq = {"start": self.clock.monotonic() + (self.delay or 0.0),
                            "n": self.nrdgs, "base": len(self.memory)}
                self._update()
        elif command.startswith(("DCV ", "OHMF ")):
            mode, value = command.split()
            self.mode, self.range_value = mode, float(value)
        elif command.startswith("NPLC "):
            self.nplc = int(command.split()[1])
            if self.fault == "config_error":
                self.err |= 64
        elif command.startswith("DELAY "):
            self.delay = float(command.split()[1])
        elif command.startswith("OCOMP "):
            self.ocomp = command.endswith("ON")
        elif command == "MEM FIFO":
            if self.fault != "stale_memory":
                self.memory = []
            self.mem = "FIFO"
        elif command == "MEM CONT":
            self.mem = "FIFO"
        elif command == "MEM OFF":
            self.mem = "OFF"
        elif command == "MFORMAT ASCII":
            if self.fault != "stale_memory":
                self.memory = []  # MFORMAT clears reading memory (C05)
        elif command.startswith("NRDGS "):
            self.nrdgs = int(command.split()[1].split(",")[0])
        elif command.startswith("INBUF "):
            self.inbuf = command == "INBUF ON"
        elif command == "MMATH STAT":
            self.math = True
        elif command == "MMATH OFF":
            self.math = False

    def query_raw(self, command: str) -> bytes:
        check_command(command)
        self.commands.append(command)
        self._resume_after_sdc()
        self._update()
        if self._acal_busy():
            self.framing_unknown = True
            raise TimeoutError(f"simulated: {command} queued behind a running autocal")
        if self.acq is not None:
            self.framing_unknown = True
            raise TimeoutError(f"simulated: {command} queued behind a running block")
        return (self._answer(command) + "\r\n").encode("ascii")

    def _answer(self, command: str) -> str:
        if command.startswith("RMEM 1,"):
            return self._rmem(int(command.split(",")[1]))
        if command.startswith("RMATH "):
            if not self.math:
                raise RuntimeError("RMATH before MMATH STAT")
            values = tuple(Decimal(format(x, " .9E").strip()) for x in self.memory)
            with localcontext() as context:
                context.prec = 60
                n = len(values)
                mean = sum(values) / n
                sdev = (sum((x - mean) ** 2 for x in values) / (n - 1)).sqrt()
                stats = {"MEAN": +mean, "SDEV": sdev, "LOWER": min(values),
                         "UPPER": max(values), "NSAMP": Decimal(n)}
            return engineering(stats[command.split()[1]])
        if command == "ERR?":
            value, self.err = self.err, 0
            return str(value)
        if command == "ERRSTR?":
            if not self.err:
                return '0,"NO ERROR"'
            bit = (self.err & -self.err).bit_length() - 1
            self.err &= self.err - 1
            return f'{100 + bit},"SIMULATED ERROR BIT {bit}"'
        if command == "NPLC?":
            return engineering(Decimal(self.nplc))
        if command == "DELAY?":
            # PRESET auto delay read 10.0001 ms (H05); DELAY 0 reads back exactly 0 (WP-02).
            delay = 0.0100001 if self.delay is None else self.delay
            return engineering(Decimal(str(delay)))
        if command == "TEMP?":
            self.temp += 0.01
            return f"{self.temp:.1f}"
        fixed = {"ID?": "HP3458A", "REV?": "9,1", "MCOUNT?": str(len(self.memory)),
                 "MSIZE?": "151552,14152", "AZERO?": str(int(self.azero)),
                 "OCOMP?": str(int(self.ocomp)), "MFORMAT?": "1", "OFORMAT?": "1",
                 "END?": "1", "INBUF?": str(int(self.inbuf)),
                 "TARM?": {"AUTO": "1", "SGL": "3", "HOLD": "4"}[self.tarm],
                 "MEM?": {"OFF": "0", "FIFO": "2"}[self.mem],
                 "LFREQ?": " 49.9832556E+00", "TERM?": "1"}  # 1 = FRONT (p. 254, WP-02 live)
        if command in fixed:
            return fixed[command]
        raise NotImplementedError(f"Not modeled: {command}")

    def _rmem(self, count: int) -> str:
        self.rmem_calls += 1
        self.mem = "OFF"  # RMEM switches memory off (p. 230)
        if count > len(self.memory):
            self.err |= 64
            self.framing_unknown = True
            raise TimeoutError("simulated: RMEM beyond MCOUNT gets no reply (C24)")
        if self.fault == "rmem_timeout" and self.rmem_calls == 1:
            self.framing_unknown = True
            raise TimeoutError("simulated: incomplete RMEM reply")
        text = encode_readings(tuple(self.memory[-count:][::-1]), "hp3458a_rev9_1")
        text = text.decode("ascii")[:-2]
        if self.fault == "corrupt_rmem" and self.rmem_calls == 1:
            text = text[:5] + ("7" if text[5] != "7" else "8") + text[6:]
        return text

    def serial_poll(self) -> int:
        self._update()
        running = self.acq is not None and not self.trigger_disabled
        return 8 | (0 if running or self._acal_busy() else 16)

    def clear_device(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("Device clear requires a recorded reason")
        self._update()
        self.clears.append(reason)
        if self.acq is not None:
            self.trigger_disabled = True
        self.framing_unknown = False

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass
