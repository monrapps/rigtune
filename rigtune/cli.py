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
    parser = argparse.ArgumentParser(description="Controle térmico de power limit NVIDIA para Linux/HiveOS")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("list", help="Lista GPUs e faixas de potência (somente leitura)")
    check = subparsers.add_parser("check", help="Valida configuração; opcionalmente consulta hardware")
    check.add_argument("--config", required=True)
    check.add_argument("--hardware", action="store_true", help="Confere IDs, suporte e limites reais")
    run = subparsers.add_parser("run", help="Inicia o controle")
    run.add_argument("--config", required=True)
    run.add_argument("--dry-run", action="store_true", help="Simula decisões sem alterar GPUs")
    run.add_argument("--once", action="store_true", help="Executa somente um ciclo (mantém warmup e proteções)")
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
                raise NvidiaError("Nenhuma GPU NVIDIA encontrada")
            for gpu in readings:
                print("GPU {} | {} | {} | PCI {} | temp={} C | PL={} W | faixa={}–{} W".format(
                    gpu.index, gpu.uuid, gpu.name, gpu.pci_bus_id, gpu.temperature_c,
                    gpu.power_limit_w, gpu.power_min_w, gpu.power_max_w,
                ))
            return 0
        config = load_config(args.config)
        if args.command == "check":
            if args.hardware:
                backend = NvidiaSmi(config.command_timeout_s)
                bind_gpus(config, backend.read_all())
            print("Configuração válida para {} GPU(s){}".format(
                len(config.gpus), "; hardware conferido" if args.hardware else " (sem consultar hardware)",
            ))
            return 0
        if not args.dry_run and os.geteuid() != 0:
            raise ConfigError("Execute com sudo/root para aplicar PL, ou use --dry-run")
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
                             "simulação" if args.dry_run else "controle ativo", len(managed))
                while not stop.is_set():
                    started = time.monotonic()
                    healthy = runner.cycle()
                    if args.once:
                        return 0 if healthy else 1
                    elapsed = time.monotonic() - started
                    if elapsed > config.poll_interval_s:
                        logging.warning("Ciclo levou %.1fs, excedendo intervalo %.1fs", elapsed, config.poll_interval_s)
                    stop.wait(max(0, config.poll_interval_s - elapsed))
            finally:
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)
                logging.info("Simulação encerrada" if args.dry_run else
                             "Controle encerrado; último PL aplicado permanece em vigor")
        return 0
    except ConfigError as exc:
        logging.error("Configuração: %s", exc)
        return 2
    except (NvidiaError, OSError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
