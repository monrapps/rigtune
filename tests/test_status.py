import json
import os
import tempfile
import unittest
from unittest.mock import Mock

from rigtune.runtime import RigRunner, bind_gpus
from rigtune.status import StatusWriter
from tests.helpers import FakeBackend, configuration, sample


class StatusTests(unittest.TestCase):
    def test_snapshot_contains_confirmed_write_and_replaces_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'status.json')
            backend = FakeBackend([sample(temp=70)])
            managed = bind_gpus(configuration(), backend.read_all())
            runner = RigRunner(backend, managed, clock=lambda: 0,
                               status=StatusWriter(path, clock=lambda: 1000))
            runner.cycle()
            with open(path) as handle:
                status = json.load(handle)
            self.assertEqual(status['gpus'][0]['power_limit_w'], 125)
            self.assertEqual(status['updated_at'], 1000)
            self.assertTrue(status['healthy'])
            runner.cycle()
            self.assertEqual(os.listdir(directory), ['status.json'])

    def test_failed_write_or_missing_gpu_cannot_publish_healthy_status(self):
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                backend = FakeBackend([sample(temp=70)])
                managed = bind_gpus(configuration(), backend.read_all())
                backend.write_errors.add(managed[0].uuid)
                if missing:
                    backend.readings = []
                path = os.path.join(directory, 'status.json')
                with self.assertLogs('rigtune', level='WARNING'):
                    RigRunner(backend, managed, status=StatusWriter(path)).cycle()
                with open(path) as handle:
                    self.assertFalse(json.load(handle)['healthy'])

    def test_optional_status_failure_does_not_interrupt_power_control(self):
        backend = FakeBackend([sample(temp=70)])
        managed = bind_gpus(configuration(), backend.read_all())
        status = Mock()
        status.write.side_effect = OSError('disk full')
        with self.assertLogs('rigtune', level='WARNING'):
            self.assertTrue(RigRunner(backend, managed, status=status).cycle())
        self.assertEqual(backend.writes[0][1], 125)

    def test_dry_run_never_publishes_virtual_limits(self):
        backend = FakeBackend([sample(temp=70)])
        managed = bind_gpus(configuration(), backend.read_all())
        status = Mock()
        RigRunner(backend, managed, dry_run=True, status=status).cycle()
        status.write.assert_not_called()
        self.assertEqual(backend.writes, [])
