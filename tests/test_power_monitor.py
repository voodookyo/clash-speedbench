"""Tests for native-clock suspend/resume detection (speedbench_power).

These tests falsify the real failure modes without sleeping, suspending,
reading wall-clock time or printing raw clock values.  Native Linux and
Windows semantics remain unverified off those operating systems; the smoke
test exercises whichever real binding the current OS provides.
"""
import importlib
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import speedbench_power as power


class PowerClockErrorTest(unittest.TestCase):
    def test_message_is_fixed_and_hides_native_details(self):
        error = power.PowerClockError("SECRET-native-detail", 123456789)
        self.assertEqual(str(error), "power clock sampling failed")
        self.assertNotIn("SECRET", str(error))
        self.assertNotIn("123456789", str(error))

    def test_bare_construction_has_fixed_message(self):
        self.assertEqual(str(power.PowerClockError()),
                         "power clock sampling failed")


class SampleValidationTest(unittest.TestCase):
    def test_inverted_interval_fails(self):
        with self.assertRaises(power.PowerClockError):
            power.validate_sample((10, 5))

    def test_bools_are_rejected_as_endpoints(self):
        for sample in ((True, 0), (0, True), (True, False)):
            with self.subTest(sample=sample):
                with self.assertRaises(power.PowerClockError):
                    power.validate_sample(sample)

    def test_non_integer_endpoints_are_rejected(self):
        for sample in ((0.5, 1.5), ("a", 1), (None, 1)):
            with self.subTest(sample=sample):
                with self.assertRaises(power.PowerClockError):
                    power.validate_sample(sample)

    def test_non_two_tuple_is_rejected(self):
        for sample in ([0, 5], 0, (0, 1, 2), "01"):
            with self.subTest(sample=sample):
                with self.assertRaises(power.PowerClockError):
                    power.validate_sample(sample)

    def test_negative_endpoints_are_allowed(self):
        self.assertEqual(power.validate_sample((-100, 0)), (-100, 0))
        self.assertEqual(power.validate_sample((-5_000_000, -1_000_000)),
                         (-5_000_000, -1_000_000))

    def test_zero_length_interval_is_valid(self):
        self.assertEqual(power.validate_sample((0, 0)), (0, 0))
        self.assertEqual(power.validate_sample((7, 7)), (7, 7))


class BracketValidationTest(unittest.TestCase):
    def test_awake_after_before_awake_before_fails(self):
        with self.assertRaises(power.PowerClockError):
            power._interval(100, 200, 50)

    def test_negative_native_reading_fails(self):
        for args in ((-1, 100, 100), (100, -1, 100), (100, 100, -1)):
            with self.subTest(args=args):
                with self.assertRaises(power.PowerClockError):
                    power._interval(*args)

    def test_bool_native_reading_fails(self):
        with self.assertRaises(power.PowerClockError):
            power._interval(True, 100, 100)
        with self.assertRaises(power.PowerClockError):
            power._interval(0, False, 100)

    def test_non_integer_native_reading_fails(self):
        with self.assertRaises(power.PowerClockError):
            power._interval(1.0, 100, 100)
        with self.assertRaises(power.PowerClockError):
            power._interval("0", 100, 100)

    def test_equal_endpoints_pass(self):
        self.assertEqual(power._interval(1000, 1000, 1000), (0, 0))

    def test_interval_formula_allows_negative_low(self):
        # low = inclusive - awake_after; high = inclusive - awake_before.
        self.assertEqual(power._interval(100, 100, 300), (100 - 300, 100 - 100))
        self.assertEqual(power._interval(100, 100, 300), (-200, 0))


class ResumeGuardConstructionTest(unittest.TestCase):
    def test_non_callable_clock_fails(self):
        for clock in (None, "clock", 42, (0, 0)):
            with self.subTest(clock=clock):
                with self.assertRaises(power.PowerClockError):
                    power.ResumeGuard(clock)

    def test_invalid_threshold_fails(self):
        for threshold in (-1, True, 1.5, "100", None):
            with self.subTest(threshold=threshold):
                with self.assertRaises(power.PowerClockError):
                    power.ResumeGuard(lambda: (0, 0), threshold_ns=threshold)

    def test_default_threshold_matches_constant(self):
        self.assertEqual(power.ResumeGuard.DEFAULT_THRESHOLD_NS, 250_000_000)
        guard = power.ResumeGuard(lambda: (0, 0))
        self.assertEqual(guard.threshold_ns, 250_000_000)


class ResumeGuardIdleTest(unittest.TestCase):
    def test_poll_when_idle_returns_none_without_native_reads(self):
        calls = []

        def clock():
            calls.append(1)
            return (0, 0)

        guard = power.ResumeGuard(clock)
        self.assertIsNone(guard.poll())
        for _ in range(10):
            self.assertIsNone(guard.poll())
        self.assertEqual(calls, [])

    def test_idle_after_disarm_returns_none_without_native_reads(self):
        calls = []

        def clock():
            calls.append(1)
            return (0, 0)

        guard = power.ResumeGuard(clock)
        guard.arm("job_a")
        guard.disarm("job_a")
        calls.clear()
        self.assertIsNone(guard.poll())
        self.assertEqual(calls, [])


class ResumeGuardArmTest(unittest.TestCase):
    def test_arm_samples_exactly_once_and_stores_job(self):
        samples = iter([(0, 0), (5_000_000, 5_000_000)])
        calls = []

        def clock():
            calls.append(1)
            return next(samples)

        guard = power.ResumeGuard(clock)
        guard.arm("job_a")
        self.assertEqual(calls, [1])
        self.assertEqual(guard.armed_job, "job_a")
        # The stored baseline is the first sample, so a poll hitting the
        # second sample shows a 5ms increase.
        self.assertIsNone(guard.poll())
        self.assertEqual(calls, [1, 1])

    def test_failing_clock_leaves_no_armed_job(self):
        def clock():
            raise power.PowerClockError()

        guard = power.ResumeGuard(clock)
        with self.assertRaises(power.PowerClockError):
            guard.arm("job_a")
        self.assertIsNone(guard.armed_job)
        # A failed arm must not leave the guard sampling on poll.
        self.assertIsNone(guard.poll())

    def test_unexpected_clock_exception_becomes_power_clock_error(self):
        def clock():
            raise RuntimeError("native blew up")

        guard = power.ResumeGuard(clock)
        with self.assertRaises(power.PowerClockError):
            guard.arm("job_a")
        self.assertIsNone(guard.armed_job)

    def test_invalid_sample_leaves_no_armed_job(self):
        guard = power.ResumeGuard(lambda: (5, 1))
        with self.assertRaises(power.PowerClockError):
            guard.arm("job_a")
        self.assertIsNone(guard.armed_job)
        self.assertIsNone(guard.poll())

    def test_failed_rearm_clears_previous_job(self):
        state = {"fail": False}

        def clock():
            if state["fail"]:
                raise power.PowerClockError()
            return (0, 0)

        guard = power.ResumeGuard(clock)
        guard.arm("job_a")
        self.assertEqual(guard.armed_job, "job_a")
        state["fail"] = True
        with self.assertRaises(power.PowerClockError):
            guard.arm("job_b")
        self.assertIsNone(guard.armed_job)

    def test_arm_rejects_invalid_job_ids(self):
        guard = power.ResumeGuard(lambda: (0, 0))
        for bad in (None, 123, "", b"job", 0, ["job"], {"job": 1}):
            with self.subTest(bad=bad):
                with self.assertRaises(power.PowerClockError):
                    guard.arm(bad)

    def test_disarm_only_clears_matching_job(self):
        guard = power.ResumeGuard(lambda: (0, 0))
        guard.arm("job_a")
        guard.arm("job_b")
        guard.disarm("job_a")
        self.assertEqual(guard.armed_job, "job_b")

    def test_disarm_unknown_job_is_noop(self):
        guard = power.ResumeGuard(lambda: (0, 0))
        guard.arm("job_a")
        guard.disarm("other")
        self.assertEqual(guard.armed_job, "job_a")


class ResumeGuardPollTest(unittest.TestCase):
    def test_normal_elapsed_time_does_not_detect_suspend(self):
        state = {"offset": 0}

        def clock():
            offset = state["offset"]
            state["offset"] += 1_000_000
            return (offset, offset + 2_000_000)

        guard = power.ResumeGuard(clock)
        guard.arm("job_a")
        for _ in range(5):
            self.assertIsNone(guard.poll())

    def test_unchanged_offset_survives_arbitrary_descheduling(self):
        # Same interval returned repeatedly: no accumulated suspend, so no
        # trigger regardless of how much real time passes between polls.
        guard = power.ResumeGuard(lambda: (0, 0))
        guard.arm("job_a")
        for _ in range(5):
            self.assertIsNone(guard.poll())

    def test_long_descheduling_widens_interval_not_offset(self):
        seq = iter([(0, 0), (-10_000_000_000, 10_000_000_000)])

        def clock():
            return next(seq)

        guard = power.ResumeGuard(clock)
        guard.arm("job_a")
        # current.low - baseline.high = -10s; a wide bracket cannot prove
        # suspend.
        self.assertIsNone(guard.poll())

    def test_conservative_uncertainty_with_negative_low(self):
        guard = power.ResumeGuard(lambda: (-500_000, 1_000_000))
        guard.arm("job_a")
        self.assertIsNone(guard.poll())

    def test_threshold_boundary_equal_does_not_trigger(self):
        seq = iter([(0, 0), (250_000_000, 250_000_000)])
        guard = power.ResumeGuard(lambda: next(seq))
        guard.arm("job_a")
        self.assertIsNone(guard.poll())

    def test_threshold_boundary_above_triggers(self):
        seq = iter([(0, 0), (250_000_001, 250_000_001)])
        guard = power.ResumeGuard(lambda: next(seq))
        guard.arm("job_a")
        self.assertEqual(guard.poll(), "job_a")

    def test_positive_low_gap_uses_baseline_high(self):
        # baseline high = 1000; current low = 300_000_000 -> gap > threshold.
        seq = iter([(0, 1000), (300_000_000, 300_000_000)])
        guard = power.ResumeGuard(lambda: next(seq))
        guard.arm("job_a")
        self.assertEqual(guard.poll(), "job_a")

    def test_confirmed_increase_returns_once_then_stays_idle(self):
        seq = iter([(0, 0), (1_000_000_000, 1_000_000_000)])
        guard = power.ResumeGuard(lambda: next(seq))
        guard.arm("job_a")
        self.assertEqual(guard.poll(), "job_a")
        calls = []

        def counting_clock():
            calls.append(1)
            return (1_000_000_000, 1_000_000_000)

        guard._clock = counting_clock
        for _ in range(10):
            self.assertIsNone(guard.poll())
        self.assertEqual(calls, [])

    def test_immediate_arm_then_resume_triggers(self):
        seq = iter([(0, 0), (2_000_000_000, 2_000_000_000)])
        guard = power.ResumeGuard(lambda: next(seq))
        guard.arm("job_a")
        self.assertEqual(guard.poll(), "job_a")

    def test_immediate_arm_then_small_resume_does_not_trigger(self):
        seq = iter([(0, 0), (100_000_000, 100_000_000)])
        guard = power.ResumeGuard(lambda: next(seq))
        guard.arm("job_a")
        self.assertIsNone(guard.poll())

    def test_new_job_after_resume_uses_fresh_baseline(self):
        seq = iter([
            (0, 0),                          # arm job_a
            (1_000_000_000, 1_000_000_000),  # poll job_a -> trigger
            (0, 0),                          # arm job_b (fresh offset 0)
            (0, 0),                          # poll job_b -> no suspend
        ])
        guard = power.ResumeGuard(lambda: next(seq))
        guard.arm("job_a")
        self.assertEqual(guard.poll(), "job_a")
        guard.arm("job_b")
        self.assertIsNone(guard.poll())

    def test_threshold_zero_triggers_only_on_positive_increase(self):
        sequence = iter([(0, 0), (1, 1)])
        trigger = power.ResumeGuard(lambda: next(sequence), threshold_ns=0)
        trigger.arm("job_a")
        self.assertEqual(trigger.poll(), "job_a")

        quiet = power.ResumeGuard(lambda: (0, 0), threshold_ns=0)
        quiet.arm("job_a")
        self.assertIsNone(quiet.poll())

    def test_invalid_sample_at_poll_fails_and_keeps_arm(self):
        seq = iter([(0, 0), (5, 1)])
        guard = power.ResumeGuard(lambda: next(seq))
        guard.arm("job_a")
        with self.assertRaises(power.PowerClockError):
            guard.poll()
        self.assertEqual(guard.armed_job, "job_a")

    def test_clock_error_at_poll_does_not_disarm_and_can_recover(self):
        state = {"fail": False, "sample": (0, 0)}

        def clock():
            if state["fail"]:
                raise power.PowerClockError()
            return state["sample"]

        guard = power.ResumeGuard(clock)
        guard.arm("job_a")
        state["fail"] = True
        with self.assertRaises(power.PowerClockError):
            guard.poll()
        self.assertEqual(guard.armed_job, "job_a")
        state["fail"] = False
        state["sample"] = (1_000_000_000, 1_000_000_000)
        self.assertEqual(guard.poll(), "job_a")

    def test_repeated_polls_after_trigger_never_repeat(self):
        state = {"armed": True}

        def clock():
            return (0, 0) if state["armed"] else (1_000_000_000, 1_000_000_000)

        guard = power.ResumeGuard(clock)
        guard.arm("job_a")
        state["armed"] = False
        results = [guard.poll() for _ in range(50)]
        self.assertEqual(results[0], "job_a")
        self.assertTrue(all(result is None for result in results[1:]))


class ResumeGuardThreadSafetyTest(unittest.TestCase):
    def test_older_failed_arm_cannot_erase_replacement(self):
        entered = threading.Event()
        release = threading.Event()
        replacement_read = threading.Event()
        attempts = []
        errors = []

        def clock():
            attempts.append(True)
            if len(attempts) == 1:
                entered.set()
                if not release.wait(2):
                    raise AssertionError('fixture release timed out')
                raise power.PowerClockError()
            replacement_read.set()
            return (0, 0)

        guard = power.ResumeGuard(clock)
        def older():
            try:
                guard.arm('older')
            except power.PowerClockError:
                errors.append('expected')
        old = threading.Thread(target=older)
        new = threading.Thread(target=lambda: guard.arm('replacement'))
        old.start()
        try:
            self.assertTrue(entered.wait(1))
            new.start()
            # The pre-fix implementation allowed this read to overtake the
            # old failure, which then erased the already registered new job.
            replacement_read.wait(.1)
        finally:
            release.set()
            old.join(2)
            if new.ident is not None:
                new.join(2)
        self.assertFalse(old.is_alive())
        self.assertFalse(new.is_alive())
        self.assertEqual(errors, ['expected'])
        self.assertEqual(guard.armed_job, 'replacement')

    def test_concurrent_arm_disarm_poll_do_not_corrupt_state(self):
        guard = power.ResumeGuard(lambda: (0, 0))
        errors = []

        def worker(fn):
            try:
                for _ in range(200):
                    fn()
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        jobs = (
            ("arm", lambda: guard.arm("job_a")),
            ("disarm", lambda: guard.disarm("job_a")),
            ("poll", lambda: guard.poll()),
        )
        threads = []
        for _name, fn in jobs:
            for _ in range(2):
                thread = threading.Thread(target=worker, args=(fn,))
                threads.append(thread)
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])

    def test_concurrent_triggers_return_the_job_at_most_once(self):
        ready = threading.Barrier(8)
        state = {"sample": (0, 0)}
        guard = power.ResumeGuard(lambda: state["sample"])
        guard.arm("job_a")
        self.assertIsNone(guard.poll())

        state["sample"] = (1_000_000_000, 1_000_000_000)
        results = []
        results_lock = threading.Lock()

        def poller():
            ready.wait()
            for _ in range(50):
                result = guard.poll()
                if result is not None:
                    with results_lock:
                        results.append(result)

        threads = [threading.Thread(target=poller) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, ["job_a"])


class NativeClockDispatchTest(unittest.TestCase):
    def test_unsupported_platform_fails_closed(self):
        with mock.patch.object(power.sys, "platform", "plan9"):
            with self.assertRaises(power.PowerClockError):
                power.native_clock()

    def test_dispatch_selects_platform_builder(self):
        sentinel = object()
        with mock.patch.object(power.sys, "platform", "darwin"), \
             mock.patch.object(power, "_darwin_power_clock", return_value=sentinel) as builder:
            self.assertIs(power.native_clock(), sentinel)
            builder.assert_called_once_with()
        with mock.patch.object(power.sys, "platform", "linux"), \
             mock.patch.object(power, "_linux_power_clock", return_value=sentinel):
            self.assertIs(power.native_clock(), sentinel)
        with mock.patch.object(power.sys, "platform", "win32"), \
             mock.patch.object(power, "_windows_power_clock", return_value=sentinel):
            self.assertIs(power.native_clock(), sentinel)

    def test_import_does_not_initialize_ctypes_libraries(self):
        with mock.patch.object(power.ctypes, "CDLL", side_effect=AssertionError("eager CDLL")), \
             mock.patch.object(power.ctypes, "WinDLL", create=True,
                               side_effect=AssertionError("eager WinDLL")):
            importlib.reload(power)
        self.assertTrue(callable(power.native_clock))


class LinuxBindingTest(unittest.TestCase):
    def _boot(self):
        return object()

    def _mono(self):
        return object()

    def test_missing_getter_fails(self):
        import time
        with mock.patch.object(time, "clock_gettime_ns", None, create=True):
            with self.assertRaises(power.PowerClockError):
                power._linux_power_clock()

    def test_missing_boottime_constant_fails(self):
        import time
        with mock.patch.object(time, "CLOCK_BOOTTIME", None, create=True):
            with self.assertRaises(power.PowerClockError):
                power._linux_power_clock()

    def test_missing_monotonic_constant_fails(self):
        import time
        with mock.patch.object(time, "CLOCK_MONOTONIC", None, create=True):
            with self.assertRaises(power.PowerClockError):
                power._linux_power_clock()

    def test_sample_brackets_boottime_against_monotonic(self):
        import time
        boot, mono = self._boot(), self._mono()
        seen = []

        def fake_getter(clock_id):
            seen.append(clock_id)
            return {mono: 100 if seen.count(mono) == 1 else 300,
                    boot: 1_000_000_000}[clock_id]

        with mock.patch.object(time, "clock_gettime_ns", fake_getter), \
             mock.patch.object(time, "CLOCK_BOOTTIME", boot, create=True), \
             mock.patch.object(time, "CLOCK_MONOTONIC", mono):
            clock = power._linux_power_clock()
        self.assertEqual(clock(), (1_000_000_000 - 300, 1_000_000_000 - 100))
        self.assertEqual(seen, [mono, boot, mono])

    def test_raw_errors_are_normalized(self):
        import time
        boot, mono = self._boot(), self._mono()

        with mock.patch.object(time, "clock_gettime_ns",
                               mock.Mock(side_effect=OSError("nope"))), \
             mock.patch.object(time, "CLOCK_BOOTTIME", boot, create=True), \
             mock.patch.object(time, "CLOCK_MONOTONIC", mono):
            clock = power._linux_power_clock()
            with self.assertRaises(power.PowerClockError):
                clock()

    def test_negative_and_bool_raw_readings_fail(self):
        import time
        boot, mono = self._boot(), self._mono()
        for bad in (-1, True, 1.5):
            with self.subTest(bad=bad):
                def fake_getter(clock_id, _bad=bad):
                    return _bad

                with mock.patch.object(time, "clock_gettime_ns", fake_getter), \
                     mock.patch.object(time, "CLOCK_BOOTTIME", boot, create=True), \
                     mock.patch.object(time, "CLOCK_MONOTONIC", mono):
                    clock = power._linux_power_clock()
                    with self.assertRaises(power.PowerClockError):
                        clock()


class DarwinBindingTest(unittest.TestCase):
    def test_validate_timebase_accepts_valid_scale(self):
        self.assertEqual(power._validate_timebase(0, 1, 1), (1, 1))
        self.assertEqual(power._validate_timebase(0, 125, 3), (125, 3))

    def test_validate_timebase_rejects_bad_scale_or_return(self):
        for args in ((-1, 1, 1), (0, 0, 1), (0, 1, 0), (0, True, 1),
                     (0, 1, True), (0, 1.0, 1), (0, "1", 1)):
            with self.subTest(args=args):
                with self.assertRaises(power.PowerClockError):
                    power._validate_timebase(*args)

    def test_interval_converts_ticks_to_integer_ns(self):
        ticks = iter([6, 9, 12])
        clock = power._darwin_interval(lambda: next(ticks),
                                       lambda: next(ticks), 2, 3)
        # 6*2//3=4, 9*2//3=6, 12*2//3=8 -> (6-8, 6-4)=(-2, 2).
        self.assertEqual(clock(), (-2, 2))

    def test_interval_rejects_invalid_timebase_without_zero_division(self):
        for numer, denom in ((0, 0), (1, 0), (0, 1), (-1, 1), (1, -1)):
            with self.subTest(numer=numer, denom=denom):
                with self.assertRaises(power.PowerClockError):
                    power._darwin_interval(lambda: 1, lambda: 1, numer, denom)

    def test_interval_rejects_bad_raw_ticks(self):
        for bad in (-1, True, 1.5):
            with self.subTest(bad=bad):
                clock = power._darwin_interval(lambda: bad, lambda: 1, 1, 1)
                with self.assertRaises(power.PowerClockError):
                    clock()

    def test_interval_rejects_inverted_bracket(self):
        ticks = iter([12, 9, 6])
        clock = power._darwin_interval(lambda: next(ticks),
                                       lambda: next(ticks), 1, 1)
        with self.assertRaises(power.PowerClockError):
            clock()

    def test_interval_normalizes_native_call_errors(self):
        clock = power._darwin_interval(mock.Mock(side_effect=OSError("x")),
                                       lambda: 1, 1, 1)
        with self.assertRaises(power.PowerClockError):
            clock()

    def test_interval_requires_callables(self):
        with self.assertRaises(power.PowerClockError):
            power._darwin_interval(None, lambda: 1, 1, 1)

    def test_missing_symbols_fail_closed(self):
        with self.assertRaises(power.PowerClockError):
            power._darwin_power_clock(library=object())

    def test_default_binding_uses_fixed_libsystem_path(self):
        fake = mock.Mock()
        fake.mach_absolute_time.return_value = 5
        fake.mach_continuous_time.return_value = 6

        def fake_timebase(pointer):
            pointer._obj.numer = 1
            pointer._obj.denom = 1
            return 0

        fake.mach_timebase_info.side_effect = fake_timebase

        with mock.patch.object(power.ctypes, "CDLL", return_value=fake) as ctor:
            clock = power._darwin_power_clock()
        ctor.assert_called_once_with(power._DARWIN_LIBRARY)
        self.assertEqual(clock(), (1, 1))


class WindowsBindingTest(unittest.TestCase):
    def test_query_nonzero_32bit_bool_is_not_truncated_to_a_byte(self):
        ctypes = power.ctypes
        @ctypes.CFUNCTYPE(ctypes.c_uint64)
        def tick():
            return 1000
        @ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.POINTER(ctypes.c_uint64))
        def query(pointer):
            pointer[0] = 10_000_000
            return 0x100  # Valid nonzero Win32 BOOL, but its low byte is zero.
        kernel = mock.Mock(GetTickCount64=tick, QueryUnbiasedInterruptTime=query)
        self.assertEqual(power._windows_power_clock(library=kernel)(), (0, 0))

    def test_interval_unit_conversion_ms_and_100ns(self):
        awake = iter([10_000, 10_100])
        clock = power._windows_interval(lambda: next(awake), lambda: 2_000)
        low, high = clock()
        self.assertEqual(low, 2_000 * 1_000_000 - 10_100 * 100)
        self.assertEqual(high, 2_000 * 1_000_000 - 10_000 * 100)
        self.assertLessEqual(low, high)

    def test_interval_rejects_bad_raw_values(self):
        for bad in (-1, True, 1.5):
            with self.subTest(bad=bad):
                clock = power._windows_interval(lambda: bad, lambda: 1)
                with self.assertRaises(power.PowerClockError):
                    clock()
            with self.subTest(read_inclusive=bad):
                clock = power._windows_interval(lambda: 0, lambda: bad)
                with self.assertRaises(power.PowerClockError):
                    clock()

    def test_interval_normalizes_awake_errors(self):
        clock = power._windows_interval(mock.Mock(side_effect=OSError("x")),
                                        lambda: 1)
        with self.assertRaises(power.PowerClockError):
            clock()

    def test_interval_requires_callables(self):
        with self.assertRaises(power.PowerClockError):
            power._windows_interval(None, lambda: 1)

    def test_missing_symbols_fail_closed(self):
        with self.assertRaises(power.PowerClockError):
            power._windows_power_clock(library=object())

    def test_default_binding_uses_kernel32_with_last_error(self):
        fake = mock.Mock()
        fake.GetTickCount64.return_value = 1_000

        def fake_query(pointer):
            pointer._obj.value = 10_000_000
            return True

        fake.QueryUnbiasedInterruptTime.side_effect = fake_query

        with mock.patch.object(power.ctypes, "WinDLL", create=True,
                               return_value=fake) as ctor:
            clock = power._windows_power_clock()
        ctor.assert_called_once_with("kernel32", use_last_error=True)
        self.assertEqual(clock(), (0, 0))

    def test_bool_false_from_query_fails(self):
        fake = mock.Mock()
        fake.GetTickCount64.return_value = 1_000
        fake.QueryUnbiasedInterruptTime.return_value = False
        with mock.patch.object(power.ctypes, "WinDLL", create=True,
                               return_value=fake):
            clock = power._windows_power_clock()
        with self.assertRaises(power.PowerClockError):
            clock()


class NativeSampleSmokeTest(unittest.TestCase):
    """Exercise the real binding on the current supported OS only.

    Never sleeps, suspends or prints raw clock values.  Linux and Windows
    semantics remain unverified when running elsewhere.
    """

    def test_current_platform_sample_is_a_valid_integer_bracket(self):
        if sys.platform not in ("darwin", "win32") and not sys.platform.startswith("linux"):
            self.skipTest("no supported native power clock on this OS")
        clock = power.native_clock()
        low, high = clock()
        self.assertIsInstance(low, int)
        self.assertIsInstance(high, int)
        self.assertNotIsInstance(low, bool)
        self.assertNotIsInstance(high, bool)
        self.assertLessEqual(low, high)
        # A running machine can show a tiny negative low from jitter only.
        self.assertLess(abs(low), 10 ** 18)
        self.assertLess(abs(high), 10 ** 18)


if __name__ == "__main__":
    unittest.main()
