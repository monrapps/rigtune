"""Configuração JSON estrita; apenas biblioteca padrão, Python 3.6+."""

import json
import math
import re
from typing import NamedTuple


UUID_PATTERN = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


class ConfigError(ValueError):
    pass


DEFAULTS = {
    "hysteresis_c": 2,
    "critical_temp_c": 75,
    "step_up_w": 3,
    "step_down_w": 5,
    "fine_step_w": 1,
    "fine_band_c": 5,
    "settle_time_s": 60,
    "warmup_s": 60,
    "trend_window_s": 30,
    "max_rise_c_per_min": 0.5,
    "temperature_alpha": 0.3,
    "min_utilization_pct": 30,
    "sensor_failure_limit": 3,
    "recovery_s": 60,
}
REQUIRED = {"power_min_w", "power_max_w", "target_temp_c"}
GPU_KEYS = set(DEFAULTS) | REQUIRED


class GpuConfig(NamedTuple):
    id: str
    power_min_w: int
    power_max_w: int
    target_temp_c: float
    hysteresis_c: float
    critical_temp_c: float
    step_up_w: int
    step_down_w: int
    fine_step_w: int
    fine_band_c: float
    settle_time_s: float
    warmup_s: float
    trend_window_s: float
    max_rise_c_per_min: float
    temperature_alpha: float
    min_utilization_pct: float
    sensor_failure_limit: int
    recovery_s: float


class Config(NamedTuple):
    poll_interval_s: float
    command_timeout_s: float
    gpus: tuple


def _object(value, allowed, location):
    if not isinstance(value, dict):
        raise ConfigError("{} deve ser um objeto JSON".format(location))
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError("{}: campos desconhecidos: {}".format(location, ", ".join(sorted(unknown))))


def _number(data, key, low, high, integer=False):
    value = data[key]
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not low <= value <= high
            or (integer and not isinstance(value, int))):
        kind = "inteiro" if integer else "número finito"
        raise ConfigError("{} deve ser {} entre {} e {}".format(key, kind, low, high))


def parse_config(data):
    _object(data, {"poll_interval_s", "command_timeout_s", "defaults", "gpus"}, "config")
    poll = data.get("poll_interval_s", 5)
    timeout = data.get("command_timeout_s", 10)
    _number({"poll_interval_s": poll}, "poll_interval_s", 1, 3600)
    _number({"command_timeout_s": timeout}, "command_timeout_s", 1, 60)
    defaults = data.get("defaults", {})
    _object(defaults, GPU_KEYS, "defaults")
    entries = data.get("gpus")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("gpus deve ser uma lista não vazia de objetos com id explícito")
    result = []
    seen = set()
    for entry in entries:
        _object(entry, GPU_KEYS | {"id"}, "gpu")
        selector = entry.get("id")
        if isinstance(selector, bool) or not isinstance(selector, (int, str)):
            raise ConfigError("id deve ser um índice NVIDIA não negativo ou UUID completo GPU-...")
        selector = str(selector)
        if not (re.fullmatch(r"[0-9]+", selector) or UUID_PATTERN.fullmatch(selector)):
            raise ConfigError("id inválido: {}".format(selector))
        selector = str(int(selector)) if selector.isdigit() else selector.lower()
        if selector in seen:
            raise ConfigError("GPU duplicada: {}".format(selector))
        seen.add(selector)
        values = dict(DEFAULTS, **defaults)
        values.update({key: value for key, value in entry.items() if key != "id"})
        missing = REQUIRED - set(values)
        if missing:
            raise ConfigError("GPU {}: faltam {}".format(selector, ", ".join(sorted(missing))))
        for key in ("power_min_w", "power_max_w", "step_up_w", "step_down_w", "fine_step_w"):
            _number(values, key, 1, 2000, integer=True)
        _number(values, "target_temp_c", 1, 110)
        _number(values, "hysteresis_c", 0, 20)
        _number(values, "critical_temp_c", 1, 120)
        _number(values, "fine_band_c", 0.1, 40)
        _number(values, "temperature_alpha", 0.01, 1)
        _number(values, "min_utilization_pct", 0, 100)
        _number(values, "sensor_failure_limit", 1, 100, integer=True)
        _number(values, "warmup_s", 0, 3600)
        _number(values, "max_rise_c_per_min", 0, 20)
        for key in ("settle_time_s", "trend_window_s", "recovery_s"):
            _number(values, key, poll, 3600)
        if values["power_min_w"] > values["power_max_w"]:
            raise ConfigError("power_min_w deve ser <= power_max_w")
        if values["critical_temp_c"] <= values["target_temp_c"] + values["hysteresis_c"]:
            raise ConfigError("critical_temp_c deve superar target_temp_c + hysteresis_c")
        if values["fine_band_c"] <= values["hysteresis_c"]:
            raise ConfigError("fine_band_c deve superar hysteresis_c")
        if values["fine_step_w"] > min(values["step_up_w"], values["step_down_w"]):
            raise ConfigError("fine_step_w não pode superar step_up_w ou step_down_w")
        result.append(GpuConfig(id=selector, **values))
    return Config(poll, timeout, tuple(result))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("Campo JSON duplicado: {}".format(key))
        result[key] = value
    return result


def load_config(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return parse_config(json.load(handle, object_pairs_hook=_unique_object))
    except (OSError, ValueError) as exc:
        raise ConfigError("{}: {}".format(path, exc)) from exc
