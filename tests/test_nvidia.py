import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from rigtune.nvidia import NvidiaError, NvidiaSmi, parse_readings
from tests.helpers import UUID_A, UUID_B


def csv_row(uuid=UUID_A, temperature="60", power="130", name="NVIDIA Test"):
    return '0, {}, "{}", 00000000:01:00.0, {}, 123.45, {}, 80, 200, 90, 95\n'.format(
        uuid, name, temperature, power,
    )


class NvidiaTests(unittest.TestCase):
    def test_csv_handles_quoted_names_and_unavailable_numbers(self):
        reading = parse_readings(csv_row(name="Test, GPU", temperature="N/A", power="[Not Supported]"))[0]
        self.assertEqual(reading.name, "Test, GPU")
        self.assertIsNone(reading.temperature_c)
        self.assertIsNone(reading.power_limit_w)
        self.assertEqual(reading.memory_utilization_pct, 95)

    def test_nonfinite_and_impossible_temperature_is_missing(self):
        for value in ("NaN", "inf", "-1", "200", "[GPU is lost]"):
            with self.subTest(value=value):
                self.assertIsNone(parse_readings(csv_row(temperature=value))[0].temperature_c)

    def test_malformed_or_duplicate_identity_is_rejected(self):
        for output in ("error message", csv_row(uuid="N/A"), csv_row() + csv_row()):
            with self.subTest(output=output), self.assertRaises(NvidiaError):
                parse_readings(output)

    @patch("rigtune.nvidia.subprocess.run")
    def test_timeout_and_nonzero_exit(self, run):
        run.side_effect = subprocess.TimeoutExpired("nvidia-smi", 10)
        with self.assertRaises(NvidiaError):
            NvidiaSmi().read_all()
        run.side_effect = None
        run.return_value = subprocess.CompletedProcess([], 1, "", "permission denied")
        with self.assertRaisesRegex(NvidiaError, "permission denied"):
            NvidiaSmi().read_all()

    @patch("rigtune.nvidia.subprocess.run")
    def test_write_uses_uuid_no_shell_and_readback(self, run):
        run.side_effect = [subprocess.CompletedProcess([], 0, "OK", ""),
                           subprocess.CompletedProcess([], 0, csv_row(power="125"), "")]
        self.assertEqual(NvidiaSmi().set_power(UUID_A, 125).power_limit_w, 125)
        write = run.call_args_list[0]
        self.assertEqual(write[0][0], ["nvidia-smi", "--id=" + UUID_A, "--power-limit=125"])
        self.assertNotIn("shell", write[1])
        self.assertEqual(write[1]["timeout"], 10)
        self.assertEqual(write[1]["env"]["LC_ALL"], "C")
        self.assertIn("--query-gpu=", run.call_args_list[1][0][0][2])

    @patch.object(NvidiaSmi, "_run")
    def test_ignored_write_or_wrong_gpu_is_rejected(self, run):
        for output in (csv_row(power="130"), csv_row(power="N/A"), csv_row(uuid=UUID_B, power="125")):
            with self.subTest(output=output), self.assertRaises(NvidiaError):
                run.side_effect = ["OK", output]
                NvidiaSmi().set_power(UUID_A, 125)

    def test_real_subprocess_protocol_with_fake_nvidia_smi(self):
        # Exercita argumentos e subprocesso real, sem acesso ao hardware.
        with tempfile.TemporaryDirectory() as directory:
            executable = os.path.join(directory, "nvidia-smi")
            script = "#!/bin/sh\ncase \"$2\" in\n --power-limit=125) exit 0 ;;\nesac\ncat <<'CSV'\n{}CSV\n".format(
                csv_row(power="125")
            )
            with open(executable, "w") as handle:
                handle.write(script)
            os.chmod(executable, 0o700)
            backend = NvidiaSmi(executable=executable)
            self.assertEqual(backend.read_all()[0].uuid, UUID_A)
            self.assertEqual(backend.set_power(UUID_A, 125).power_limit_w, 125)
