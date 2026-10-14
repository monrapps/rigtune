# Repository Guidelines

## Purpose and Architecture

RigTune controls NVIDIA GPU power limits on Linux/HiveOS. The objective is maximum hashrate within the thermal target, adapting power as ambient temperature changes. Hash/W is not the optimization goal.

## Project Structure and Module Organization

- `rigtune/control.py`: per-GPU decisions, temperature smoothing, trend detection, settling, and recovery.
- `rigtune/config.py`: strict JSON validation; `nvidia.py`: driver queries and confirmed writes.
- `rigtune/runtime.py`: GPU selection, process locks, and execution; `cli.py` and `__main__.py`: command entry points.
- `rigtune/hiveos.py`: optional API synchronization; `status.py`: atomic controller snapshots.
- `tests/`: standard-library tests and shared fake hardware in `helpers.py`.
- `config*.example.json`: generic and rig-specific configuration templates.
- `deploy/`: systemd units; `docs/hiveos.md`: optional integration; `reports/`: hardware observations.

## Build, Test, and Development Commands

Run from the repository root. No build or external dependencies are required; retain Python 3.6 compatibility.

- `python3 -m unittest discover -v`: run the hardware-free test suite.
- `python3 -m rigtune check --config config.example.json`: validate configuration offline.
- `python3 -m rigtune list`: inspect NVIDIA GPUs and their power ranges.
- `python3 -m rigtune check --config config.json --hardware`: validate against connected GPUs.
- `python3 -m rigtune run --config config.json --dry-run --once`: inspect one decision cycle without hardware writes.
- `systemd-analyze verify deploy/rigtune.service`: validate service changes.

## Coding Style and Naming Conventions

Use four-space indentation, `snake_case` functions and variables, `PascalCase` classes, and uppercase constants. Preserve unit suffixes (`_w`, `_c`, `_s`). Write repository content in English; match the user's language in conversation. No formatter or linter is configured. Separate controller decisions from subprocess and filesystem operations.

## Testing Guidelines

Use `unittest`, `unittest.mock`, `test_*.py` modules, and descriptive `test_*` methods. Exercise thermal lag, zero tolerance, power bounds, failed telemetry, write confirmation, and recovery when relevant. Use fake backends and controlled clocks; automated tests must not change real GPUs. No numeric coverage threshold is configured.

## Commit and Pull Request Guidelines

Follow the existing Conventional Commits style with English, lowercase imperative subjects, such as `fix: handle missing temperature readings`. Keep commits focused. PRs should explain behavior changes, reference relevant issues, list validation commands/results, and describe deployment impact. Update affected examples and documentation.

## Configuration and Deployment

Keep local configurations and tokens ignored. Prefer UUIDs and driver-reported power ranges. Preserve settling and critical-temperature protection. Current profiles use 62 °C, zero tolerance, and 60-second settling. Deployment uses `/opt/rigtune`, `/etc/rigtune/config.json`, and `rigtune.service`; hardware writes require root. Preserve active configurations. HiveOS synchronization runs separately; API failures must never block thermal control.
