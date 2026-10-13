"""Stepwise control with thermal settling and independent state for each GPU."""

from collections import deque
from typing import NamedTuple, Optional


class Decision(NamedTuple):
    power_w: Optional[float]
    reason: str


class ThermalController:
    def __init__(self, config):
        self.config = config
        self.filtered_temp_c = None
        self.trend_c_per_min = None
        self.history = deque()
        self.last_change_at = None
        self.loaded_since = None
        self.failures = 0
        self.recovering = False
        self.cool_since = None

    def changed(self, now):
        """Start settling only after confirming the applied power limit."""
        self.last_change_at = now
        self.history.clear()
        self.trend_c_per_min = None

    def external_change(self, now):
        self.changed(now)
        self.loaded_since = None
        self.filtered_temp_c = None

    def write_failed(self):
        self.recovering = True
        self.cool_since = None
        self.loaded_since = None

    def _minimum(self, current, reason):
        # Do not raise a power limit already below the configured minimum.
        if current is not None and current <= self.config.power_min_w:
            return Decision(None, reason)
        return Decision(self.config.power_min_w, reason)

    def _observe(self, temp, now):
        alpha = self.config.temperature_alpha
        if self.filtered_temp_c is None:
            self.filtered_temp_c = temp
        else:
            self.filtered_temp_c += alpha * (temp - self.filtered_temp_c)
        self.history.append((now, temp))
        cutoff = now - self.config.trend_window_s
        # Retain the point before the cutoff to cover the entire window.
        while len(self.history) > 2 and self.history[1][0] <= cutoff:
            self.history.popleft()
        if len(self.history) < 2 or now - self.history[0][0] < self.config.trend_window_s:
            self.trend_c_per_min = None
            return
        origin = self.history[0][0]
        xs = [point[0] - origin for point in self.history]
        ys = [point[1] for point in self.history]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        variance = sum((x - mean_x) ** 2 for x in xs)
        self.trend_c_per_min = 60 * sum(
            (x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)
        ) / variance if variance else None

    def decide(self, reading, now):
        cfg = self.config
        current = reading.power_limit_w if reading is not None else None
        temp = reading.temperature_c if reading is not None else None

        # A critical raw reading bypasses smoothing, settling, and PL sensor failures.
        if temp is not None and temp >= cfg.critical_temp_c:
            self.recovering = True
            self.cool_since = None
            self.loaded_since = None
            self._observe(temp, now)
            return self._minimum(current, "critical_temperature")

        if temp is None or current is None:
            self.failures += 1
            self.loaded_since = None
            self.cool_since = None
            self.history.clear()
            self.filtered_temp_c = None
            self.trend_c_per_min = None
            if self.failures >= cfg.sensor_failure_limit or self.recovering:
                self.recovering = True
                return self._minimum(current, "sensor_failure_minimum")
            # Enforce the maximum even without a valid temperature reading.
            if current is not None and current > cfg.power_max_w:
                return Decision(cfg.power_max_w, "enforce_maximum")
            return Decision(None, "sensor_unavailable")

        self.failures = 0
        self._observe(temp, now)
        upper = cfg.target_temp_c + cfg.hysteresis_c
        lower = cfg.target_temp_c - cfg.hysteresis_c
        control_temp = max(temp, self.filtered_temp_c)

        if self.recovering:
            if control_temp <= upper:
                if self.cool_since is None:
                    self.cool_since = now
                if now - self.cool_since >= cfg.recovery_s:
                    self.recovering = False
                    self.cool_since = None
                    self.loaded_since = None
                else:
                    return self._minimum(current, "recovery")
            else:
                self.cool_since = None
                return self._minimum(current, "recovery")

        utilizations = [value for value in (
            reading.gpu_utilization_pct, reading.memory_utilization_pct
        ) if value is not None]
        loaded = cfg.min_utilization_pct == 0 or (
            bool(utilizations) and max(utilizations) >= cfg.min_utilization_pct
        )
        if loaded:
            if self.loaded_since is None:
                self.loaded_since = now
        else:
            self.loaded_since = None

        if current > cfg.power_max_w:
            return Decision(cfg.power_max_w, "enforce_maximum")
        if self.last_change_at is not None and now - self.last_change_at < cfg.settle_time_s:
            return Decision(None, "settling")

        near = abs(control_temp - cfg.target_temp_c) <= cfg.fine_band_c
        if control_temp > upper:
            if current <= cfg.power_min_w:
                return Decision(None, "hot_at_minimum")
            step = cfg.fine_step_w if near else cfg.step_down_w
            return Decision(max(cfg.power_min_w, current - step), "decrease_fine" if near else "decrease")

        if control_temp < lower:
            if not loaded:
                return Decision(None, "idle_or_unknown_load")
            if now - self.loaded_since < cfg.warmup_s:
                return Decision(None, "warmup")
            if self.trend_c_per_min is None:
                return Decision(None, "collecting_trend")
            if self.trend_c_per_min > cfg.max_rise_c_per_min:
                return Decision(None, "temperature_still_rising")
            if current >= cfg.power_max_w:
                return Decision(None, "cold_at_maximum")
            if current < cfg.power_min_w:
                return Decision(cfg.power_min_w, "enforce_minimum")
            step = cfg.fine_step_w if near else cfg.step_up_w
            requested = min(cfg.power_max_w, current + step)
            return Decision(requested, "increase_fine" if near else "increase")
        return Decision(None, "in_target_band")
