from rigtune.config import parse_config
from rigtune.nvidia import Reading


UUID_A = "GPU-11111111-1111-1111-1111-111111111111"
UUID_B = "GPU-22222222-2222-2222-2222-222222222222"


def configuration(**overrides):
    defaults = dict(power_min_w=100, power_max_w=160, target_temp_c=60)
    defaults.update(overrides)
    return parse_config({"defaults": defaults, "gpus": [{"id": 0}]})


def sample(temp=60, power=130, utilization=90, uuid=UUID_A, index="0", **overrides):
    reading = Reading(index, uuid, "NVIDIA Test", "00000000:01:00.0",
                      temp, 120, power, 80, 200, utilization, utilization)
    return reading._replace(**overrides)


class FakeBackend:
    def __init__(self, readings=None):
        self.readings = readings if readings is not None else [sample()]
        self.writes = []
        self.write_errors = set()
        self.read_error = None

    def read_all(self):
        if self.read_error:
            raise self.read_error
        return list(self.readings)

    def set_power(self, uuid, watts):
        from rigtune.nvidia import NvidiaError
        self.writes.append((uuid, watts))
        if uuid in self.write_errors:
            raise NvidiaError("injected write failure")
        for index, reading in enumerate(self.readings):
            if reading.uuid == uuid:
                updated = reading._replace(power_limit_w=watts)
                self.readings[index] = updated
                return updated
        raise NvidiaError("missing GPU")
