"""Optional HiveOS profile synchronization, isolated from the thermal controller."""

import copy
import json
import logging
import math
import os
import re
import signal
import stat
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import ConfigError, UUID_PATTERN, _number, _object, _unique_object
from .nvidia import NvidiaError, NvidiaSmi
from .runtime import GpuLocks


LOG = logging.getLogger("rigtune.hiveos")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULTS = {
    "api_url": "https://api2.hiveos.farm/api/v2",
    "token_file": "/etc/rigtune/hiveos.token",
    "status_file": "/run/rigtune/status.json",
    "rig_config_file": "/hive-config/rig.conf",
    "sync_interval_s": 60,
    "retry_max_s": 900,
    "request_timeout_s": 10,
    "max_status_age_s": 20,
    "max_worker_age_s": 120,
}


class HiveError(RuntimeError):
    pass


def load_settings(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle, object_pairs_hook=_unique_object)
        _object(data, set(DEFAULTS) | {"farm_id", "worker_id"}, "hiveos")
        settings = dict(DEFAULTS, **data)
        for key in ("farm_id", "worker_id"):
            if key not in settings:
                raise ConfigError("Missing HiveOS {}".format(key))
            _number(settings, key, 1, 2 ** 53, integer=True)
        for key, low, high in (
            ("sync_interval_s", 60, 86400), ("retry_max_s", 60, 86400),
            ("request_timeout_s", 1, 60), ("max_status_age_s", 1, 60),
            ("max_worker_age_s", 10, 300),
        ):
            _number(settings, key, low, high)
        if settings["retry_max_s"] < settings["sync_interval_s"]:
            raise ConfigError("retry_max_s must be >= sync_interval_s")
        for key in ("token_file", "status_file", "rig_config_file"):
            if not isinstance(settings[key], str) or not os.path.isabs(settings[key]):
                raise ConfigError("{} must be an absolute path".format(key))
        url = settings["api_url"]
        if not isinstance(url, str):
            raise ConfigError("api_url must be an HTTPS URL")
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment
                or any(char.isspace() for char in url)):
            raise ConfigError("api_url must use HTTPS without credentials, query, or fragment")
        settings["api_url"] = url.rstrip("/")
        return settings
    except (OSError, ValueError) as exc:
        raise ConfigError("HiveOS configuration: {}".format(exc)) from exc


def read_token(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or info.st_uid != os.geteuid()):
                raise HiveError("Token file must be owned by the current user with mode 600 or 400")
            token = handle.read(4097).strip()
    except (OSError, UnicodeError) as exc:
        raise HiveError("Cannot read HiveOS token file") from exc
    if not token or len(token) > 4096 or any(ord(c) > 127 or c.isspace() for c in token):
        raise HiveError("HiveOS token file must contain only an ASCII API token")
    return token


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the bearer token to a redirect target.
        return None


class HiveClient:
    def __init__(self, settings, opener=None):
        self.settings = settings
        self.opener = opener or build_opener(NoRedirect())
        self.worker_path = "/farms/{}/workers/{}".format(settings["farm_id"], settings["worker_id"])

    def request(self, method, path, payload=None):
        token = read_token(self.settings["token_file"])
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode("utf-8")
        request = Request(self.settings["api_url"] + path, data=body, method=method, headers={
            "Authorization": "Bearer " + token, "Accept": "application/json",
            "Content-Type": "application/json", "User-Agent": "RigTune/0.1.0",
        })
        try:
            with self.opener.open(request, timeout=self.settings["request_timeout_s"]) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise HiveError("HiveOS response exceeds the size limit")
                result = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(result, dict):
                    raise HiveError("Unexpected HiveOS response structure")
                return result
        except HTTPError as exc:
            # Responses may contain account configuration or credentials. Never log them.
            reason = ""
            if exc.code == 403:
                try:
                    error = json.loads(exc.read(4096).decode("utf-8"))
                    if isinstance(error, dict) and error.get("message") == "Cannot use API keys to access free farms":
                        reason = ": this free farm does not permit API keys"
                except (OSError, ValueError):
                    pass
            exc.close()
            raise HiveError("HiveOS API returned HTTP {}{}".format(exc.code, reason)) from exc
        except (URLError, OSError, ValueError) as exc:
            raise HiveError("HiveOS request failed or returned invalid JSON") from exc

    def get_worker(self):
        result = self.request("GET", self.worker_path)
        result = result.get("data", result)
        if not isinstance(result, dict):
            raise HiveError("Unexpected HiveOS worker response")
        return result

    def update_worker(self, patch):
        return self.request("PATCH", self.worker_path, patch)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def pci_address(value):
    if not isinstance(value, str):
        raise HiveError("Missing PCI address")
    match = re.fullmatch(r"(?:(?P<domain>[0-9a-fA-F]{4,8}):)?(?P<bus>[0-9a-fA-F]{2}):"
                         r"(?P<device>[0-9a-fA-F]{2})\.(?P<function>[0-7])", value)
    if not match:
        raise HiveError("Invalid PCI address")
    return "{:04x}:{}:{}.{}".format(int(match.group("domain") or "0", 16),
                                  match.group("bus").lower(), match.group("device").lower(),
                                  match.group("function"))


def load_snapshot(settings, now):
    try:
        with open(settings["status_file"], encoding="utf-8") as handle:
            data = json.loads(handle.read(MAX_RESPONSE_BYTES + 1))
    except (OSError, ValueError) as exc:
        raise HiveError("Controller status is unavailable or invalid") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or data.get("healthy") is not True:
        raise HiveError("Controller is not healthy; skipping cloud synchronization")
    timestamp = data.get("updated_at")
    if not _finite(timestamp) or not -5 <= now - timestamp <= settings["max_status_age_s"]:
        raise HiveError("Controller status is stale; skipping cloud synchronization")
    gpus = data.get("gpus")
    if not isinstance(gpus, list) or not gpus:
        raise HiveError("Controller status has no GPUs")
    seen, buses = set(), set()
    for gpu in gpus:
        if not isinstance(gpu, dict):
            raise HiveError("Invalid GPU status")
        uuid = gpu.get("uuid")
        if not isinstance(uuid, str) or not UUID_PATTERN.fullmatch(uuid) or uuid.lower() in seen:
            raise HiveError("Invalid or duplicate GPU UUID in controller status")
        bus = pci_address(gpu.get("pci_bus_id"))
        if bus in buses:
            raise HiveError("Duplicate PCI address in controller status")
        seen.add(uuid.lower())
        buses.add(bus)
        fields = [gpu.get(key) for key in ("power_min_w", "power_limit_w", "power_max_w",
                                           "temperature_c", "critical_temp_c")]
        if not all(_finite(value) for value in fields):
            raise HiveError("Controller status has missing or invalid telemetry")
        low, power, high, temp, critical = fields
        if not (0 < low <= power <= high and power == int(power) and 0 <= temp < critical <= 120):
            raise HiveError("GPU is outside synchronization limits")
        if gpu.get("recovering") is not False:
            raise HiveError("GPU is recovering; skipping cloud synchronization")
    return data


def verify_local_worker(settings):
    try:
        with open(settings["rig_config_file"], encoding="utf-8") as handle:
            # Parse only the numeric ID; never source rig.conf or log its secrets.
            matches = re.findall(r'^RIG_ID=[\"\']?([0-9]+)[\"\']?\s*$', handle.read(), re.M)
    except (OSError, UnicodeError) as exc:
        raise HiveError("Cannot verify the local HiveOS worker ID") from exc
    if len(matches) != 1 or int(matches[0]) != settings["worker_id"]:
        raise HiveError("Configured worker_id does not match this HiveOS rig")


def profile(worker):
    overclock, config = worker.get("overclock"), worker.get("oc_config")
    if not isinstance(overclock, dict) or not isinstance(config, dict):
        raise HiveError("HiveOS did not return an active overclock configuration")
    algo = overclock.get("algo")
    if algo:
        entries = config.get("by_algo", [])
        if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
            raise HiveError("Invalid per-algorithm overclock configuration")
        matches = [i for i, item in enumerate(entries) if item.get("algo") == algo]
        if len(matches) != 1:
            raise HiveError("Cannot identify the active algorithm profile")
        selected = entries[matches[0]]
        return selected, algo, matches[0]
    selected = config.get("default", {})
    if not isinstance(selected, dict):
        raise HiveError("Invalid default overclock configuration")
    return selected, None, None


def power_values(selected, count):
    nvidia = selected.get("nvidia", {})
    if not isinstance(nvidia, dict):
        raise HiveError("Invalid NVIDIA overclock configuration")
    value = nvidia.get("power_limit", "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise HiveError("Unexpected HiveOS power_limit format")
    tokens = value.split() or ["0"]
    if len(tokens) > count or any(not re.fullmatch(r"[0-9]+", token) for token in tokens):
        raise HiveError("Invalid NVIDIA power limit vector")
    values = [int(token) for token in tokens]
    return values + [values[-1]] * (count - len(values))


def build_patch(worker, snapshot, settings, now):
    if worker.get("id") != settings["worker_id"]:
        raise HiveError("HiveOS returned a different worker")
    farm_id = worker.get("farm_id")
    if farm_id is not None and farm_id != settings["farm_id"]:
        raise HiveError("HiveOS returned a different farm")
    stats = worker.get("stats") or {}
    timestamp = stats.get("stats_time")
    if (stats.get("online") is not True or not _finite(timestamp)
            or not -5 <= now - timestamp <= settings["max_worker_age_s"]):
        raise HiveError("HiveOS worker is offline or stale; refusing to queue an update")
    inventory = worker.get("gpu_info")
    if not isinstance(inventory, list) or any(not isinstance(gpu, dict) for gpu in inventory):
        raise HiveError("HiveOS GPU inventory is unavailable")
    cards = [gpu for gpu in inventory if gpu.get("brand") == "nvidia"]
    indices = [gpu.get("index") for gpu in cards]
    if (not cards or any(type(index) is not int or index < 0 for index in indices)
            or len(indices) != len(set(indices))):
        raise HiveError("HiveOS NVIDIA GPU indices are missing or ambiguous")
    cards.sort(key=lambda gpu: gpu["index"])
    by_bus = {}
    for i, card in enumerate(cards):
        bus = pci_address(card.get("bus_id"))
        if bus in by_bus:
            raise HiveError("HiveOS has duplicate NVIDIA PCI addresses")
        by_bus[bus] = i
    selected, algo, position = profile(worker)
    source = selected
    nvidia = selected.get("nvidia") or {}
    if not isinstance(nvidia, dict):
        raise HiveError("Invalid NVIDIA overclock configuration")
    if not nvidia.get("power_limit"):
        # A per-algorithm profile can inherit its power vector. Use the API's
        # resolved profile, never turn an omitted limit into a factory reset.
        source = worker["overclock"]
        resolved = source.get("nvidia") or {}
        if not isinstance(resolved, dict):
            raise HiveError("Invalid resolved NVIDIA overclock configuration")
        if not resolved.get("power_limit") and len(snapshot["gpus"]) != len(cards):
            raise HiveError("Cannot preserve uncontrolled GPUs with unspecified power limits")
    original = power_values(source, len(cards))
    updated = list(original)
    for gpu in snapshot["gpus"]:
        bus = pci_address(gpu["pci_bus_id"])
        if bus not in by_bus:
            raise HiveError("A controlled GPU is absent from the HiveOS inventory")
        index = by_bus[bus]
        remote_uuid = cards[index].get("uuid") or (cards[index].get("details") or {}).get("uuid")
        if remote_uuid and remote_uuid.lower() != gpu["uuid"].lower():
            raise HiveError("HiveOS GPU UUID does not match the local GPU")
        updated[index] = int(gpu["power_limit_w"])
    if original == updated:
        return None
    if worker.get("commands"):
        raise HiveError("HiveOS has pending commands; waiting before another update")
    value = " ".join(str(watts) for watts in updated)
    if algo:
        # Preserve every other algorithm entry even if the API replaces this array.
        entries = copy.deepcopy(worker["oc_config"]["by_algo"])
        entries[position].setdefault("nvidia", {})["power_limit"] = value
        patch_config = {"by_algo": entries}
    else:
        patch_config = {"default": {"nvidia": {"power_limit": value}}}
    return {"oc_apply_mode": "merge", "oc_config": patch_config}


def fingerprint(snapshot):
    return sorted((gpu["uuid"].lower(), pci_address(gpu["pci_bus_id"]), gpu["power_limit_w"],
                   gpu["power_min_w"], gpu["power_max_w"], gpu["critical_temp_c"])
                  for gpu in snapshot["gpus"])


class HiveSync:
    def __init__(self, settings, client=None, backend=None, clock=time.time):
        self.settings = settings
        self.client = client or HiveClient(settings)
        self.backend = backend or NvidiaSmi(settings["request_timeout_s"])
        self.clock = clock

    def cycle(self, dry_run=False):
        verify_local_worker(self.settings)
        load_snapshot(self.settings, self.clock())
        worker = self.client.get_worker()
        snapshot = load_snapshot(self.settings, self.clock())
        readings = {reading.uuid.lower(): reading for reading in self.backend.read_all()}
        for gpu in snapshot["gpus"]:
            reading = readings.get(gpu["uuid"].lower())
            if (reading is None or reading.power_limit_w != gpu["power_limit_w"]
                    or pci_address(reading.pci_bus_id) != pci_address(gpu["pci_bus_id"])
                    or not _finite(reading.temperature_c)
                    or not 0 <= reading.temperature_c < gpu["critical_temp_c"]):
                raise HiveError("GPU telemetry changed or is unsafe; retry with a fresh snapshot")
        patch = build_patch(worker, snapshot, self.settings, self.clock())
        if patch is None:
            LOG.info("HiveOS power limits already match confirmed controller readings")
            return None
        latest = load_snapshot(self.settings, self.clock())
        if fingerprint(latest) != fingerprint(snapshot):
            raise HiveError("Controller power limits changed during the API request; retry later")
        if dry_run:
            LOG.info("[DRY-RUN] HiveOS profile update: %s", json.dumps(patch, sort_keys=True))
            return patch
        self.client.update_worker(patch)
        confirmed = self.client.get_worker()
        desired = copy.deepcopy(worker)
        if "default" in patch["oc_config"]:
            desired["oc_config"].setdefault("default", {}).setdefault("nvidia", {}).update(
                patch["oc_config"]["default"]["nvidia"])
        else:
            desired["oc_config"]["by_algo"] = patch["oc_config"]["by_algo"]
        if profile(confirmed) != profile(desired):
            raise HiveError("HiveOS profile readback did not match; recheck before retrying")
        LOG.info("HiveOS power limit profile updated and read back; the agent may reapply overclock settings")
        return patch


def serve(sync, settings, stop, dry_run=False, once=False):
    delay = settings["sync_interval_s"]
    while not stop.is_set():
        try:
            sync.cycle(dry_run)
            delay = settings["sync_interval_s"]
            healthy = True
        except (HiveError, NvidiaError, OSError, ValueError, TypeError, KeyError) as exc:
            # Unexpected schema fields fail closed; never dump the API response.
            message = str(exc) if isinstance(exc, HiveError) else type(exc).__name__
            LOG.warning("HiveOS synchronization skipped: %s", message)
            healthy = False
        if once:
            return 0 if healthy else 1
        stop.wait(delay)
        if not healthy:
            delay = min(delay * 2, settings["retry_max_s"])
    return 0


def run_sync(path, dry_run=False, once=False):
    settings = load_settings(path)
    stop = threading.Event()
    handlers = {}
    with GpuLocks(["worker-{}".format(settings["worker_id"])], "/run/lock/rigtune-hiveos"):
        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                handlers[sig] = signal.signal(sig, lambda signum, frame: stop.set())
            return serve(HiveSync(settings), settings, stop, dry_run, once)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
