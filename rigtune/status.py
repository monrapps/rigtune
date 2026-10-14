"""Atomic, optional status snapshots for readers outside the control loop."""

import json
import os
import tempfile
import time


class StatusWriter:
    def __init__(self, path, clock=time.time):
        self.path = os.path.abspath(path)
        self.clock = clock

    def write(self, managed, readings, healthy):
        gpus = []
        for gpu in managed:
            reading = readings.get(gpu.uuid.lower())
            if reading is None:
                continue
            gpus.append({
                "uuid": gpu.uuid,
                "pci_bus_id": reading.pci_bus_id,
                "temperature_c": reading.temperature_c,
                "power_limit_w": reading.power_limit_w,
                "power_min_w": gpu.config.power_min_w,
                "power_max_w": gpu.config.power_max_w,
                "critical_temp_c": gpu.config.critical_temp_c,
                "state": gpu.last_reason,
                "recovering": gpu.controller.recovering,
            })
        payload = {
            "version": 1, "updated_at": self.clock(), "pid": os.getpid(),
            "healthy": bool(healthy and len(gpus) == len(managed)), "gpus": gpus,
        }
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".rigtune-status-", dir=directory)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle, allow_nan=False)
                handle.write("\n")
                os.fchmod(handle.fileno(), 0o644)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
