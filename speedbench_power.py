"""Suspend/resume detection backed by real platform clocks.

The only supported technique is a three-read bracket::

    awake_before -> inclusive -> awake_after

``awake`` clocks do not advance while the machine is suspended; ``inclusive``
clocks keep advancing through suspend.  With the inclusive reading taken
between the two awake readings the accumulated suspend offset is bracketed by::

    low  = inclusive - awake_after
    high = inclusive - awake_before

Sampling jitter can make either endpoint negative, so the interval - not a
single number - is the result.  A long descheduling gap only widens that
interval; it can never by itself prove a suspend happened.

Platform clocks are the real native APIs:

* Linux  : CLOCK_BOOTTIME (inclusive) vs CLOCK_MONOTONIC (awake).
* Darwin : mach_continuous_time (inclusive) vs mach_absolute_time (awake).
* Windows: GetTickCount64 (inclusive, milliseconds) vs
           QueryUnbiasedInterruptTime (awake, 100ns).  The inclusive clock is
           coarse, so the default threshold allows for its resolution.

This module performs no wall-clock reads, no boot-time/duration persistence,
no HTTP, no UI, no process killing and starts no background threads.
"""
from __future__ import annotations

import ctypes
import sys
import threading
from typing import Callable, Optional, Tuple


_ERROR_MESSAGE = "power clock sampling failed"

_NS_PER_MILLISECOND = 1_000_000
_NS_PER_100NS = 100

_DARWIN_LIBRARY = "/usr/lib/libSystem.B.dylib"

PowerSample = Tuple[int, int]
PowerClock = Callable[[], PowerSample]


class PowerClockError(Exception):
    """Generic power-clock failure with a fixed, detail-free message.

    Any supplied arguments are discarded so raw native error strings or
    sampled clock values can never leak through the exception text.
    """

    def __init__(self, *_ignored) -> None:
        super().__init__(_ERROR_MESSAGE)


def _validate_native_raw(value) -> None:
    """A native reading must be an exact nonnegative int (never a bool)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PowerClockError()


def _validate_interval_endpoint(value) -> None:
    """An interval endpoint is an exact int (negative jitter is allowed)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise PowerClockError()


def _validate_bracket(awake_before: int, inclusive: int, awake_after: int) -> None:
    """Validate the awake_before -> inclusive -> awake_after bracket."""
    _validate_native_raw(awake_before)
    _validate_native_raw(inclusive)
    _validate_native_raw(awake_after)
    if awake_after < awake_before:
        raise PowerClockError()


def _interval(awake_before: int, inclusive: int, awake_after: int) -> PowerSample:
    """Bracket the suspend offset in nanoseconds; endpoints may be negative."""
    _validate_bracket(awake_before, inclusive, awake_after)
    return inclusive - awake_after, inclusive - awake_before


def validate_sample(sample) -> PowerSample:
    """Validate and return a (low, high) suspend-offset interval."""
    if not isinstance(sample, tuple) or len(sample) != 2:
        raise PowerClockError()
    low, high = sample
    _validate_interval_endpoint(low)
    _validate_interval_endpoint(high)
    if low > high:
        raise PowerClockError()
    return low, high


def _read_clock(clock: PowerClock) -> PowerSample:
    """Invoke a clock callable, normalising any failure to PowerClockError."""
    try:
        return clock()
    except PowerClockError:
        raise
    except Exception:
        raise PowerClockError()


def native_clock() -> PowerClock:
    """Return a callable sampling the current platform's suspend interval.

    Raises PowerClockError on unsupported platforms, missing native APIs or
    unsupported clock layouts.  There is deliberately no heuristic fallback.
    """
    platform = sys.platform
    if platform == "darwin":
        return _darwin_power_clock()
    if platform.startswith("linux"):
        return _linux_power_clock()
    if platform == "win32":
        return _windows_power_clock()
    raise PowerClockError()


def _linux_power_clock() -> PowerClock:
    """CLOCK_BOOTTIME includes suspend; CLOCK_MONOTONIC does not."""
    import time

    getter = getattr(time, "clock_gettime_ns", None)
    boot_id = getattr(time, "CLOCK_BOOTTIME", None)
    mono_id = getattr(time, "CLOCK_MONOTONIC", None)
    if not callable(getter) or boot_id is None or mono_id is None:
        raise PowerClockError()

    def sample() -> PowerSample:
        try:
            awake_before = getter(mono_id)
            inclusive = getter(boot_id)
            awake_after = getter(mono_id)
        except PowerClockError:
            raise
        except Exception:
            raise PowerClockError()
        return _interval(awake_before, inclusive, awake_after)

    return sample


class _MachTimebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


def _validate_timebase(rc, numer, denom) -> Tuple[int, int]:
    """Validate mach_timebase_info output: KERN_SUCCESS and a nonzero scale."""
    if rc != 0:
        raise PowerClockError()
    if isinstance(numer, bool) or not isinstance(numer, int) or numer <= 0:
        raise PowerClockError()
    if isinstance(denom, bool) or not isinstance(denom, int) or denom <= 0:
        raise PowerClockError()
    return numer, denom


def _darwin_interval(absolute, continuous, numer: int, denom: int) -> PowerClock:
    """Build the interval sampler from typed mach tick callables.

    mach_continuous_time includes suspend while mach_absolute_time excludes
    it; both share the same timebase and are converted to integer ns.
    """
    if not callable(absolute) or not callable(continuous):
        raise PowerClockError()
    numer, denom = _validate_timebase(0, numer, denom)

    def sample() -> PowerSample:
        try:
            awake_before_ticks = absolute()
            inclusive_ticks = continuous()
            awake_after_ticks = absolute()
        except PowerClockError:
            raise
        except Exception:
            raise PowerClockError()
        _validate_native_raw(awake_before_ticks)
        _validate_native_raw(inclusive_ticks)
        _validate_native_raw(awake_after_ticks)
        awake_before = (awake_before_ticks * numer) // denom
        inclusive = (inclusive_ticks * numer) // denom
        awake_after = (awake_after_ticks * numer) // denom
        return _interval(awake_before, inclusive, awake_after)

    return sample


def _darwin_power_clock(library=None) -> PowerClock:
    """Bind the fixed libSystem mach clocks and validate their timebase."""
    try:
        lib = ctypes.CDLL(_DARWIN_LIBRARY) if library is None else library
        absolute = lib.mach_absolute_time
        continuous = lib.mach_continuous_time
        timebase = lib.mach_timebase_info
    except (OSError, AttributeError):
        raise PowerClockError()

    absolute.argtypes = []
    absolute.restype = ctypes.c_uint64
    continuous.argtypes = []
    continuous.restype = ctypes.c_uint64
    timebase.argtypes = [ctypes.POINTER(_MachTimebase)]
    timebase.restype = ctypes.c_int

    info = _MachTimebase()
    try:
        rc = timebase(ctypes.byref(info))
    except (OSError, ctypes.ArgumentError, TypeError):
        raise PowerClockError()
    numer, denom = _validate_timebase(rc, info.numer, info.denom)
    return _darwin_interval(absolute, continuous, numer, denom)


def _windows_interval(read_awake, read_inclusive) -> PowerClock:
    """Build the interval sampler from an awake and an inclusive callable.

    ``read_awake`` returns 100-ns units excluding suspend/hibernate;
    ``read_inclusive`` returns milliseconds including suspend.  The coarse
    inclusive clock's resolution is allowed for by the default guard threshold.
    """
    if not callable(read_awake) or not callable(read_inclusive):
        raise PowerClockError()

    def sample() -> PowerSample:
        try:
            awake_before_100ns = read_awake()
            inclusive_ms = read_inclusive()
            awake_after_100ns = read_awake()
        except PowerClockError:
            raise
        except Exception:
            raise PowerClockError()
        _validate_native_raw(awake_before_100ns)
        _validate_native_raw(inclusive_ms)
        _validate_native_raw(awake_after_100ns)
        awake_before = awake_before_100ns * _NS_PER_100NS
        inclusive = inclusive_ms * _NS_PER_MILLISECOND
        awake_after = awake_after_100ns * _NS_PER_100NS
        return _interval(awake_before, inclusive, awake_after)

    return sample


def _windows_power_clock(library=None) -> PowerClock:
    """Bind kernel32 and build the GetTickCount64/QueryUnbiased sampler."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if library is None else library
        get_tick_count64 = kernel32.GetTickCount64
        query_unbiased = kernel32.QueryUnbiasedInterruptTime
    except (OSError, AttributeError):
        raise PowerClockError()

    get_tick_count64.argtypes = []
    get_tick_count64.restype = ctypes.c_uint64
    query_unbiased.argtypes = [ctypes.POINTER(ctypes.c_uint64)]
    query_unbiased.restype = ctypes.c_int32  # Win32 BOOL is 32 bits, not C bool.

    def read_awake() -> int:
        value = ctypes.c_uint64()
        try:
            ok = query_unbiased(ctypes.byref(value))
        except (OSError, ctypes.ArgumentError, TypeError):
            raise PowerClockError()
        if not ok:
            raise PowerClockError()
        return value.value

    def read_inclusive() -> int:
        try:
            return get_tick_count64()
        except (OSError, ctypes.ArgumentError, TypeError):
            raise PowerClockError()

    return _windows_interval(read_awake, read_inclusive)


class ResumeGuard:
    """Detect a suspend/resume while a single job is armed.

    arm() samples one native baseline immediately and stores it together with
    the job id.  If that sample fails the guard is left idle (no armed job).
    poll() never reads the clock while idle, and when the current interval
    proves an increase beyond the threshold it returns the armed job id
    exactly once and disarms atomically.  disarm() clears only a matching job.
    All state transitions are guarded by an RLock and the injected clock is
    sampled under that lock so an older failed arm cannot clear a newer job.
    """

    DEFAULT_THRESHOLD_NS = 250_000_000

    def __init__(self, clock: PowerClock,
                 threshold_ns: int = DEFAULT_THRESHOLD_NS) -> None:
        if not callable(clock):
            raise PowerClockError()
        if isinstance(threshold_ns, bool) or not isinstance(threshold_ns, int) or threshold_ns < 0:
            raise PowerClockError()
        self._clock = clock
        self._threshold_ns = threshold_ns
        self._lock = threading.RLock()
        self._baseline: Optional[PowerSample] = None
        self._job_id: Optional[str] = None

    @property
    def threshold_ns(self) -> int:
        return self._threshold_ns

    @property
    def armed_job(self) -> Optional[str]:
        with self._lock:
            return self._job_id

    def arm(self, job_id: str) -> None:
        if isinstance(job_id, bool) or not isinstance(job_id, str) or not job_id:
            raise PowerClockError()
        with self._lock:
            try:
                baseline = validate_sample(_read_clock(self._clock))
            except PowerClockError:
                self._baseline = None
                self._job_id = None
                raise
            self._baseline = baseline
            self._job_id = job_id

    def disarm(self, job_id: str) -> None:
        with self._lock:
            if self._job_id == job_id:
                self._baseline = None
                self._job_id = None

    def poll(self) -> Optional[str]:
        with self._lock:
            job_id = self._job_id
            baseline = self._baseline
            if job_id is None or baseline is None:
                return None
            current = validate_sample(_read_clock(self._clock))
            current_low, _current_high = current
            if current_low - baseline[1] > self._threshold_ns:
                self._baseline = None
                self._job_id = None
                return job_id
            return None
