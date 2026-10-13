"""Execução, isolamento de falhas por GPU e exclusão entre processos."""

import fcntl
import logging
import os
import time

from .config import ConfigError
from .control import ThermalController
from .nvidia import NvidiaError


LOG = logging.getLogger("rigtune")


class GpuLocks:
    """Arquivos persistentes: não remover ao liberar (evita corrida de inode)."""

    def __init__(self, uuids, directory="/run/lock/rigtune"):
        self.uuids = sorted(uuids)
        self.directory = directory
        self.handles = []

    def __enter__(self):
        os.makedirs(self.directory, mode=0o755, exist_ok=True)
        try:
            for uuid in self.uuids:
                path = os.path.join(self.directory, uuid.lower() + ".lock")
                fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                handle = os.fdopen(fd, "w")
                self.handles.append(handle)
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ConfigError("Já existe um RigTune controlando {}".format(uuid)) from exc
                handle.truncate(0)
                handle.write(str(os.getpid()))
                handle.flush()
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *args):
        for handle in reversed(self.handles):
            handle.close()
        self.handles.clear()


class ManagedGpu:
    def __init__(self, config, reading):
        self.uuid = reading.uuid
        self.config = config
        self.controller = ThermalController(config)
        self.last_seen_limit = reading.power_limit_w
        self.virtual_limit = reading.power_limit_w
        self.last_reason = None
        self.last_log_at = None


def bind_gpus(config, readings):
    """Valida todas as GPUs selecionadas antes de permitir qualquer escrita."""
    managed = []
    seen = set()
    for gpu_config in config.gpus:
        matches = [reading for reading in readings if gpu_config.id in (
            reading.index, reading.uuid.lower(),
        )]
        if len(matches) != 1:
            raise ConfigError("GPU {} não encontrada ou ambígua".format(gpu_config.id))
        reading = matches[0]
        if reading.uuid.lower() in seen:
            raise ConfigError("GPU {} selecionada mais de uma vez (índice/UUID)".format(reading.uuid))
        seen.add(reading.uuid.lower())
        low, high = reading.power_min_w, reading.power_max_w
        if low is None or high is None or reading.power_limit_w is None or low > high:
            raise ConfigError("GPU {}: power limit não suportado ou indisponível".format(reading.uuid))
        if not low <= gpu_config.power_min_w <= gpu_config.power_max_w <= high:
            raise ConfigError(
                "GPU {}: faixa configurada {}–{} W fora da faixa de hardware {}–{} W".format(
                    reading.uuid, gpu_config.power_min_w, gpu_config.power_max_w, low, high,
                )
            )
        managed.append(ManagedGpu(gpu_config, reading))
    return managed


class RigRunner:
    def __init__(self, backend, managed, dry_run=False, clock=time.monotonic):
        self.backend = backend
        self.managed = managed
        self.dry_run = dry_run
        self.clock = clock

    def cycle(self):
        healthy = True
        try:
            readings = {reading.uuid.lower(): reading for reading in self.backend.read_all()}
        except NvidiaError as exc:
            LOG.error("Falha de telemetria: %s", exc)
            readings = {}
            healthy = False
        for gpu in self.managed:
            now = self.clock()
            reading = readings.get(gpu.uuid.lower())
            controller = gpu.controller
            if reading is not None:
                actual = reading.power_limit_w
                if (actual is not None and gpu.last_seen_limit is not None
                        and abs(actual - gpu.last_seen_limit) > 0.5):
                    LOG.warning(
                        "%s: PL externo mudou de %s para %s W; reiniciando acomodação",
                        gpu.uuid, gpu.last_seen_limit, actual,
                    )
                    controller.external_change(now)
                    if self.dry_run:
                        gpu.virtual_limit = actual
                gpu.last_seen_limit = actual
                # Nunca mascara N/A; apenas simula os PLs nas leituras válidas.
                if self.dry_run and actual is not None:
                    reading = reading._replace(power_limit_w=gpu.virtual_limit)
            decision = controller.decide(reading, now)
            if decision.reason in (
                "critical_temperature", "sensor_failure_minimum", "sensor_unavailable",
                "hot_at_minimum", "recovery",
            ):
                healthy = False
            level = logging.INFO
            if decision.reason in ("critical_temperature", "sensor_failure_minimum", "hot_at_minimum"):
                level = logging.ERROR
            elif decision.reason == "sensor_unavailable":
                level = logging.WARNING
            if (decision.power_w is not None or decision.reason != gpu.last_reason
                    or gpu.last_log_at is None or now - gpu.last_log_at >= 60):
                LOG.log(
                    level,
                    "%s%s temp=%sC media=%sC tendencia=%sC/min consumo=%sW PL=%sW -> %sW estado=%s",
                    "[DRY-RUN] " if self.dry_run else "", gpu.uuid,
                    reading.temperature_c if reading else None,
                    _rounded(controller.filtered_temp_c), _rounded(controller.trend_c_per_min),
                    reading.power_draw_w if reading else None,
                    reading.power_limit_w if reading else None,
                    decision.power_w if decision.power_w is not None else "manter", decision.reason,
                )
                gpu.last_reason = decision.reason
                gpu.last_log_at = now
            if decision.power_w is None:
                continue
            if self.dry_run:
                gpu.virtual_limit = decision.power_w
                controller.changed(self.clock())
                continue
            try:
                confirmed = self.backend.set_power(gpu.uuid, decision.power_w)
            except NvidiaError as exc:
                LOG.error("%s: não foi possível confirmar o ajuste: %s", gpu.uuid, exc)
                controller.write_failed()
                healthy = False
                continue
            gpu.last_seen_limit = confirmed.power_limit_w
            controller.changed(self.clock())
            LOG.info("%s: PL confirmado em %s W", gpu.uuid, confirmed.power_limit_w)
        return healthy


def _rounded(value):
    return round(value, 2) if value is not None else None
