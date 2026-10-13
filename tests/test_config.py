import json
import tempfile
import unittest

from rigtune.config import ConfigError, load_config, parse_config
from tests.helpers import UUID_A, configuration, sample
from rigtune.runtime import bind_gpus


class ConfigTests(unittest.TestCase):
    def test_per_gpu_overrides_and_global_defaults(self):
        config = parse_config({
            "defaults": {"power_min_w": 100, "power_max_w": 160, "target_temp_c": 60},
            "gpus": [{"id": 0}, {"id": UUID_A, "target_temp_c": 65, "step_up_w": 2}],
        })
        self.assertEqual(config.gpus[0].step_up_w, 3)
        self.assertEqual(config.gpus[1].step_up_w, 2)
        self.assertEqual(config.gpus[1].target_temp_c, 65)
        self.assertEqual(config.gpus[1].power_max_w, 160)

    def test_invalid_values_rejected(self):
        invalid = [
            {"power_min_w": 170}, {"power_max_w": -1}, {"power_min_w": True},
            {"target_temp_c": "60"}, {"temperature_alpha": float("nan")},
            {"settle_time_s": float("inf")}, {"step_up_w": 0}, {"step_down_w": 1.5},
            {"critical_temp_c": 62}, {"fine_step_w": 4}, {"fine_band_c": 2},
            {"settle_time_s": 1}, {"min_utilization_pct": 101}, {"sensor_failure_limit": 0},
            {"target_tempp_c": 60}, {"hysteresis_c": -0.1},
        ]
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(ConfigError):
                configuration(**override)

    def test_invalid_structure_and_ids_rejected(self):
        for data in [[], {}, {"gpus": []}, {"gpus": [0]}, {"gpus": [{"id": -1}]},
                     {"gpus": [{"id": True}]}, {"gpus": [{"id": "GPU-prefix"}]},
                     {"gpus": [{"id": 0}]}, {"defaults": [], "gpus": [{"id": 0}]},
                     {"poll_intervall_s": 5}]:
            with self.subTest(data=data), self.assertRaises(ConfigError):
                parse_config(data)

    def test_duplicate_json_keys_rejected(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
            handle.write('{"gpus": [], "gpus": []}')
            handle.flush()
            with self.assertRaisesRegex(ConfigError, "duplicado"):
                load_config(handle.name)

    def test_duplicate_index_spellings_rejected(self):
        config = configuration()
        defaults = config.gpus[0]._asdict()
        defaults.pop("id")
        with self.assertRaisesRegex(ConfigError, "duplicada"):
            parse_config({"defaults": defaults, "gpus": [{"id": 0}, {"id": "00"}]})

    def test_same_gpu_by_index_and_uuid_rejected_before_writes(self):
        config = configuration()
        config = config._replace(gpus=config.gpus + (config.gpus[0]._replace(id=UUID_A.lower()),))
        with self.assertRaisesRegex(ConfigError, "mais de uma vez"):
            bind_gpus(config, [sample()])

    def test_hardware_validation(self):
        for readings in [[], [sample(power_min_w=120)], [sample(power_max_w=150)],
                         [sample(power_min_w=None)], [sample(power=None)]]:
            with self.subTest(readings=readings), self.assertRaises(ConfigError):
                bind_gpus(configuration(), readings)

    def test_example_is_valid(self):
        with open("config.example.json") as handle:
            config = parse_config(json.load(handle))
        self.assertEqual(config.gpus[0].target_temp_c, 60)
