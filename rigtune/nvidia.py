"""nvidia-smi interface: CSV, UUIDs, timeouts, and write confirmation."""

import csv
import io
import math
import os
import subprocess
from typing import NamedTuple, Optional

from .config import UUID_PATTERN


class NvidiaError(RuntimeError):
    pass


class Reading(NamedTuple):
    index: str
    uuid: str
    name: str
    pci_bus_id: str
    temperature_c: Optional[float]
    power_draw_w: Optional[float]
    power_limit_w: Optional[float]
    power_min_w: Optional[float]
    power_max_w: Optional[float]
    gpu_utilization_pct: Optional[float]
    memory_utilization_pct: Optional[float]


FIELDS = (
    "index,uuid,name,pci.bus_id,temperature.gpu,power.draw,power.limit,"
    "power.min_limit,power.max_limit,utilization.gpu,utilization.memory"
)


def _numeric(value, low=0, high=float("inf")):
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) and low <= number <= high else None


def parse_readings(output):
    readings = []
    seen = set()
    for row in csv.reader(io.StringIO(output), skipinitialspace=True):
        if not row:
            continue
        if len(row) != 11:
            raise NvidiaError("Invalid CSV response from nvidia-smi (expected 11 fields)")
        row = [field.strip() for field in row]
        index, uuid, name, pci = row[:4]
        if not index.isdigit() or not UUID_PATTERN.fullmatch(uuid):
            raise NvidiaError("Invalid GPU identity in nvidia-smi response")
        if uuid.lower() in seen:
            raise NvidiaError("Duplicate UUID in nvidia-smi response")
        seen.add(uuid.lower())
        readings.append(Reading(
            index, uuid, name, pci,
            _numeric(row[4], 0, 125), _numeric(row[5]),
            _numeric(row[6], 0.01), _numeric(row[7], 0.01), _numeric(row[8], 0.01),
            _numeric(row[9], 0, 100), _numeric(row[10], 0, 100),
        ))
    return readings


class NvidiaSmi:
    def __init__(self, timeout_s=10, executable="nvidia-smi"):
        self.timeout_s = timeout_s
        self.executable = executable

    def _run(self, args):
        environment = dict(os.environ, LC_ALL="C")
        try:
            result = subprocess.run(
                [self.executable] + args,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=self.timeout_s, env=environment,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
            raise NvidiaError("Failed to execute nvidia-smi: {}".format(exc)) from exc
        if result.returncode:
            message = (result.stderr or result.stdout).strip()[:500]
            raise NvidiaError("nvidia-smi returned {}: {}".format(result.returncode, message))
        return result.stdout

    def read_all(self):
        return parse_readings(self._run(["--query-gpu=" + FIELDS, "--format=csv,noheader,nounits"]))

    def read_one(self, uuid):
        readings = parse_readings(self._run([
            "--id=" + uuid, "--query-gpu=" + FIELDS, "--format=csv,noheader,nounits",
        ]))
        if len(readings) != 1 or readings[0].uuid.lower() != uuid.lower():
            raise NvidiaError("GPU {} missing during confirmation".format(uuid))
        return readings[0]

    def set_power(self, uuid, watts):
        if not UUID_PATTERN.fullmatch(uuid) or not math.isfinite(watts) or watts <= 0:
            raise NvidiaError("Invalid GPU identity or power value for write")
        self._run(["--id=" + uuid, "--power-limit={:g}".format(watts)])
        reading = self.read_one(uuid)
        actual = reading.power_limit_w
        if actual is None or abs(actual - watts) > 0.5:
            raise NvidiaError("GPU {}: requested {} W; readback {} W".format(uuid, watts, actual))
        return reading
