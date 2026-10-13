import argparse
import logging
import os
import signal
import sys
import threading
import time
from contextlib import ExitStack

from . import __version__
from .config import ConfigError, load_config
from .nvidia import NvidiaError, NvidiaSmi
from .runtime import GpuLocks, RigRunner, bind_gpus


def build_parser():
    parser = argparse.ArgumentParser(description="Thermal power limit control for NVIDIA GPUs on Linux/HiveOS")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("list", help="List GPUs and power ranges (read-only)")
    check = subparsers.add_parser("check", help="Validate configuration, optionally checking hardware")
    check.add_argument("--config", required=True)
    check.add_argument("--hardware", action="store_true", help="Check GPU IDs, support, and hardware limits")
    run = subparsers.add_parser("run", help="Start the controller")
    run.add_argument("--config", required=True)
    run.add_argument("--dry-run", action="store_true", help="Simulate decisions without changing GPUs")
    run.add_argument("--once", action="store_true", help="Run one cycle (preserves warmup and protections)")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "list":
            readings = NvidiaSmi().read_all()
            if not readings:
                raise NvidiaError("No NVIDIA GPUs found")
            for gpu in readings:
                print("GPU {} | {} | {} | PCI {} | temp={} C | PL={} W | range={}–{} W".format(
                    gpu.index, gpu.uuid, gpu.name, gpu.pci_bus_id, gpu.temperature_c,
                    gpu.power_limit_w, gpu.power_min_w, gpu.power_max_w,
                ))
            return 0
        config = load_config(args.config)
        if args.command == "check":
            if args.hardware:
                backend = NvidiaSmi(config.command_timeout_s)
                bind_gpus(config, backend.read_all())
            print("Configuration valid for {} GPU(s){}".format(
                len(config.gpus), "; hardware checked" if args.hardware else " (without checking hardware)",
            ))
            return 0
        if not args.dry_run and os.geteuid() != 0:
            raise ConfigError("Run with sudo/root to apply power limits, or use --dry-run")
        backend = NvidiaSmi(config.command_timeout_s)
        managed = bind_gpus(config, backend.read_all())
        stop = threading.Event()
        old_handlers = {}

        def request_stop(signum, frame):
            stop.set()

        with ExitStack() as stack:
            if not args.dry_run:
                stack.enter_context(GpuLocks([gpu.uuid for gpu in managed]))
            for sig in (signal.SIGTERM, signal.SIGINT):
                old_handlers[sig] = signal.signal(sig, request_stop)
            try:
                runner = RigRunner(backend, managed, args.dry_run)
                logging.info("RigTune %s: %s, %d GPU(s)", __version__,
                             "dry run" if args.dry_run else "controller active", len(managed))
                while not stop.is_set():
                    started = time.monotonic()
                    healthy = runner.cycle()
                    if args.once:
                        return 0 if healthy else 1
                    elapsed = time.monotonic() - started
                    if elapsed > config.poll_interval_s:
                        logging.warning("Cycle took %.1fs, exceeding interval %.1fs", elapsed, config.poll_interval_s)
                    stop.wait(max(0, config.poll_interval_s - elapsed))
            finally:
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)
                logging.info("Dry run stopped" if args.dry_run else
                             "Controller stopped; last applied power limit remains in effect")
        return 0
    except ConfigError as exc:
        logging.error("Configuration: %s", exc)
        return 2
    except (NvidiaError, OSError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
