import logging
import tempfile
import unittest

from rigtune.config import ConfigError
from rigtune.nvidia import NvidiaError
from rigtune.runtime import GpuLocks, RigRunner, bind_gpus
from tests.helpers import FakeBackend, UUID_A, UUID_B, configuration, sample


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        # Some scenarios intentionally trigger errors; keep test output readable.
        self.logger = logging.getLogger("rigtune")
        self.old_disabled = self.logger.disabled
        self.logger.disabled = True

    def tearDown(self):
        self.logger.disabled = self.old_disabled

    def runner(self, config=None, readings=None, dry_run=False):
        backend = FakeBackend(readings)
        managed = bind_gpus(config or configuration(), backend.read_all())
        runner = RigRunner(backend, managed, dry_run=dry_run, clock=lambda: 0)
        return runner, backend

    def test_dry_run_never_writes_and_keeps_virtual_limit(self):
        runner, backend = self.runner(readings=[sample(temp=70)], dry_run=True)
        runner.cycle()
        self.assertEqual(runner.managed[0].virtual_limit, 125)
        runner.clock = lambda: 60
        runner.cycle()
        self.assertEqual(runner.managed[0].virtual_limit, 120)
        self.assertEqual(backend.writes, [])
        self.assertEqual(backend.readings[0].power_limit_w, 130)

    def test_dry_run_does_not_mask_missing_power_telemetry(self):
        runner, backend = self.runner(dry_run=True)
        backend.readings = [sample(power=None)]
        self.assertFalse(runner.cycle())
        self.assertEqual(runner.managed[0].controller.failures, 1)
        self.assertEqual(backend.writes, [])

    def test_only_selected_uuid_is_modified_after_index_reorder(self):
        runner, backend = self.runner(readings=[sample(), sample(uuid=UUID_B, index="1")])
        backend.readings = [sample(temp=80, index="1"), sample(temp=80, uuid=UUID_B, index="0")]
        runner.cycle()
        self.assertEqual(backend.writes, [(UUID_A, 100)])

    def test_write_failure_isolated_and_followed_by_minimum(self):
        config = configuration()
        config = config._replace(gpus=config.gpus + (config.gpus[0]._replace(id="1"),))
        runner, backend = self.runner(config, [sample(temp=70), sample(temp=70, uuid=UUID_B, index="1")])
        backend.write_errors.add(UUID_A)
        self.assertFalse(runner.cycle())
        self.assertEqual(backend.writes, [(UUID_A, 125), (UUID_B, 125)])
        self.assertTrue(runner.managed[0].controller.recovering)
        self.assertIsNone(runner.managed[0].controller.last_change_at)
        backend.write_errors.clear()
        runner.clock = lambda: 5
        runner.cycle()
        self.assertEqual(backend.writes[-1], (UUID_A, 100))

    def test_all_read_failures_attempt_minimum_by_cached_uuid(self):
        runner, backend = self.runner()
        backend.read_error = NvidiaError("timeout")
        runner.cycle()
        runner.cycle()
        self.assertEqual(backend.writes, [])
        self.assertFalse(runner.cycle())
        self.assertEqual(backend.writes, [(UUID_A, 100)])

    def test_missing_gpu_is_not_replaced_by_same_index(self):
        runner, backend = self.runner()
        backend.readings = [sample(uuid=UUID_B)]
        for _ in range(3):
            runner.cycle()
        self.assertEqual(backend.writes, [(UUID_A, 100)])

    def test_external_change_resets_settling(self):
        runner, backend = self.runner(readings=[sample(temp=50)])
        runner.cycle()
        backend.readings = [sample(temp=50, power=140)]
        runner.clock = lambda: 65
        runner.cycle()
        self.assertEqual(backend.writes, [])
        self.assertEqual(runner.managed[0].controller.last_change_at, 65)

    def test_gpu_lock_prevents_overlap_and_releases_after_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            with GpuLocks([UUID_A], directory):
                with self.assertRaises(ConfigError):
                    with GpuLocks([UUID_A], directory):
                        self.fail("should not acquire the lock")
                with GpuLocks([UUID_B], directory):
                    pass
            with GpuLocks([UUID_A, UUID_B], directory):
                pass

    def test_partial_lock_acquisition_is_cleaned_up(self):
        with tempfile.TemporaryDirectory() as directory:
            with GpuLocks([UUID_B], directory):
                with self.assertRaises(ConfigError):
                    with GpuLocks([UUID_A, UUID_B], directory):
                        pass
                with GpuLocks([UUID_A], directory):
                    pass
