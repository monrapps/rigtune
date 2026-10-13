import math
import unittest

from rigtune.control import ThermalController
from tests.helpers import configuration, sample


class ControlTests(unittest.TestCase):
    def controller(self, **overrides):
        return ThermalController(configuration(**overrides).gpus[0])

    def warm(self, controller, temp=50, power=130, until=60, **kwargs):
        decision = None
        for now in range(0, until + 1, 5):
            decision = controller.decide(sample(temp=temp, power=power, **kwargs), now)
        return decision

    def test_band_and_boundaries_hold(self):
        for temp in (58, 59, 60, 61, 62):
            with self.subTest(temp=temp):
                decision = self.warm(self.controller(), temp)
                self.assertIsNone(decision.power_w)
                self.assertEqual(decision.reason, "in_target_band")

    def test_coarse_and_fine_steps(self):
        for temp, watts, reason in [(50, 133, "increase"), (56, 131, "increase_fine"),
                                    (70, 125, "decrease"), (64, 129, "decrease_fine")]:
            with self.subTest(temp=temp):
                decision = self.warm(self.controller(), temp)
                self.assertEqual((decision.power_w, decision.reason), (watts, reason))

    def test_zero_tolerance_adjusts_on_either_side_of_target(self):
        for temp, watts, reason in [
            (61.9, 131, "increase_fine"),
            (62, None, "in_target_band"),
            (62.1, 129, "decrease_fine"),
        ]:
            with self.subTest(temp=temp):
                controller = self.controller(target_temp_c=62, hysteresis_c=0)
                decision = self.warm(controller, temp)
                self.assertEqual((decision.power_w, decision.reason), (watts, reason))

    def test_warmup_requires_continuous_load(self):
        controller = self.controller()
        self.assertIsNone(self.warm(controller, until=55).power_w)
        controller.decide(sample(temp=50, utilization=0), 60)
        self.assertEqual(controller.decide(sample(temp=50), 65).reason, "warmup")
        self.assertEqual(controller.decide(sample(temp=50), 120).reason, "warmup")
        self.assertEqual(controller.decide(sample(temp=50), 125).power_w, 133)

    def test_settling_starts_at_confirmation_and_survives_ordinary_heat(self):
        controller = self.controller()
        self.warm(controller)
        controller.changed(63)  # comando/driver levou 3 segundos
        for now in (65, 90, 120, 122):
            self.assertEqual(controller.decide(sample(temp=70, power=133), now).reason, "settling")
        self.assertEqual(controller.decide(sample(temp=70, power=133), 123).power_w, 128)

    def test_critical_temperature_ignores_ema_wait_and_missing_power(self):
        controller = self.controller(temperature_alpha=0.01)
        self.warm(controller)
        controller.changed(60)
        for power in (133, None):
            result = controller.decide(sample(temp=75, power=power), 61)
            self.assertEqual((result.power_w, result.reason), (100, "critical_temperature"))

    def test_rising_temperature_blocks_repeated_increases(self):
        controller = self.controller()
        self.warm(controller, temp=40)
        controller.changed(60)
        for now in range(65, 126, 5):
            decision = controller.decide(sample(temp=40 + (now - 60) / 10, power=133), now)
        self.assertEqual(decision.reason, "temperature_still_rising")
        self.assertIsNone(decision.power_w)

    def test_trend_resets_after_confirmed_adjustment(self):
        controller = self.controller(settle_time_s=5)
        self.warm(controller)
        controller.changed(60)
        decision = controller.decide(sample(temp=50, power=133), 65)
        self.assertEqual(decision.reason, "collecting_trend")

    def test_raw_heat_prevents_increase_despite_low_ema(self):
        controller = self.controller(temperature_alpha=0.01)
        self.warm(controller, temp=40)
        decision = controller.decide(sample(temp=70), 65)
        self.assertEqual(decision.power_w, 125)

    def test_idle_unknown_load_and_memory_load(self):
        for gpu, memory in [(0, 0), (None, None), (None, 0)]:
            with self.subTest(gpu=gpu, memory=memory):
                decision = self.warm(self.controller(), gpu_utilization_pct=gpu, memory_utilization_pct=memory)
                self.assertEqual(decision.reason, "idle_or_unknown_load")
        decision = self.warm(self.controller(), gpu_utilization_pct=0, memory_utilization_pct=95)
        self.assertEqual(decision.power_w, 133)

    def test_hot_idle_gpu_still_decreases(self):
        self.assertEqual(self.controller().decide(sample(temp=70, utilization=0), 0).power_w, 125)

    def test_limits_and_unreachable_target(self):
        for temp, power, expected, reason in [
            (50, 159, 160, "increase"), (70, 101, 100, "decrease"),
            (50, 160, None, "cold_at_maximum"), (70, 100, None, "hot_at_minimum"),
            (60, 200, 160, "enforce_maximum"),
        ]:
            with self.subTest(temp=temp, power=power):
                result = self.warm(self.controller(), temp, power)
                self.assertEqual((result.power_w, result.reason), (expected, reason))

    def test_safety_never_raises_a_power_below_minimum(self):
        for temp in (70, 75, None):
            controller = self.controller(sensor_failure_limit=1)
            result = controller.decide(sample(temp=temp, power=90), 0)
            self.assertIsNone(result.power_w)

    def test_sensor_failure_threshold_and_recovery(self):
        controller = self.controller()
        for now in (0, 5):
            self.assertEqual(controller.decide(None, now).reason, "sensor_unavailable")
        self.assertEqual(controller.decide(None, 10).power_w, 100)
        controller.changed(10)
        self.assertEqual(controller.decide(sample(temp=50, power=100), 15).reason, "recovery")
        self.assertEqual(controller.decide(sample(temp=50, power=100), 70).reason, "recovery")
        self.assertEqual(controller.decide(sample(temp=50, power=100), 75).reason, "warmup")

    def test_isolated_sensor_faults_do_not_accumulate(self):
        controller = self.controller()
        for now in (0, 10, 20):
            self.assertEqual(controller.decide(None, now).reason, "sensor_unavailable")
            controller.decide(sample(), now + 5)
        self.assertFalse(controller.recovering)

    def test_recovery_requires_continuous_cool_readings(self):
        controller = self.controller(temperature_alpha=1)
        controller.decide(sample(temp=80), 0)
        controller.decide(sample(temp=50, power=100), 5)
        controller.decide(sample(temp=70, power=100), 60)
        self.assertEqual(controller.decide(sample(temp=50, power=100), 65).reason, "recovery")
        self.assertEqual(controller.decide(sample(temp=50, power=100), 120).reason, "recovery")
        self.assertEqual(controller.decide(sample(temp=50, power=100), 125).reason, "warmup")

    def test_critical_then_missing_sensor_remains_in_protection(self):
        controller = self.controller()
        controller.decide(sample(temp=80), 0)
        self.assertEqual(controller.decide(None, 5).power_w, 100)

    def test_failed_write_enters_protection(self):
        controller = self.controller()
        self.warm(controller)
        controller.write_failed()
        self.assertEqual(controller.decide(sample(temp=50), 65).power_w, 100)
        self.assertIsNone(controller.last_change_at)

    def test_external_change_restarts_warmup_and_settling(self):
        controller = self.controller()
        self.warm(controller)
        controller.external_change(65)
        self.assertEqual(controller.decide(sample(temp=50, power=140), 70).reason, "settling")
        self.assertIsNone(controller.trend_c_per_min)

    def test_lagging_thermal_plant_converges_with_bounded_steps(self):
        # Planta de primeira ordem: equilíbrio=25+0.25*PL e constante térmica 90s.
        controller = self.controller()
        power, temp = 100, 40
        changes, tail = [], []
        for now in range(0, 5401, 5):
            equilibrium = 25 + 0.25 * power
            temp += (equilibrium - temp) * (1 - math.exp(-5 / 90))
            decision = controller.decide(sample(temp=temp, power=power), now)
            if decision.power_w is not None:
                if changes:
                    self.assertGreaterEqual(now - changes[-1], 60)
                self.assertLessEqual(abs(decision.power_w - power), 5)
                changes.append(now)
                power = decision.power_w
            if decision.power_w is not None:
                controller.changed(now)
            self.assertTrue(100 <= power <= 160)
            if now >= 4800:
                tail.append(temp)
        self.assertGreater(len(changes), 5)
        self.assertTrue(all(58 <= value <= 62 for value in tail), (power, tail[-1]))
