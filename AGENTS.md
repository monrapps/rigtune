# Repository Guidelines

## Purpose and Architecture

RigTune controls NVIDIA GPU power limits on Linux/HiveOS. The objective is maximum hashrate within the thermal target, adapting power as ambient temperature changes. Hash/W is not the optimization goal.

## Project Structure and Module Organization

- `rigtune/control.py`: per-GPU decisions, temperature smoothing, trend detection, settling, and recovery.
- `rigtune/config.py`: strict JSON validation; `nvidia.py`: driver queries and confirmed writes.
- `rigtune/runtime.py`: GPU selection, process locks, and execution; `cli.py` and `__main__.py`: command entry points.
- `tests/`: standard-library tests and shared fake hardware in `helpers.py`.
- `config*.example.json`: generic and rig-specific configuration templates.
- `deploy/rigtune.service`: systemd unit; `reports/`: historical hardware observations; `README.md`: usage documentation.

## Build, Test, and Development Commands

Run from the repository root. There is no build step or external Python dependency; retain Python 3.6-compatible code.

- `python3 -m unittest discover -v`: run the hardware-free test suite.
- `python3 -m rigtune check --config config.example.json`: validate configuration offline.
- `python3 -m rigtune list`: inspect NVIDIA GPUs and their power ranges.
- `python3 -m rigtune check --config config.json --hardware`: validate against connected GPUs.
- `python3 -m rigtune run --config config.json --dry-run --once`: inspect one decision cycle without hardware writes.
- `systemd-analyze verify deploy/rigtune.service`: validate service changes.

## Coding Style and Naming Conventions

Use four-space indentation, `snake_case` functions and variables, `PascalCase` classes, and uppercase constants. Preserve unit suffixes such as `_w`, `_c`, and `_s`. Write documentation, comments, docstrings, and program messages in English. Match the user's language in conversation. No formatter or linter is configured. Keep controller decisions separate from subprocess and filesystem operations.

## Testing Guidelines

Use `unittest`, `unittest.mock`, `test_*.py` modules, and descriptive `test_*` methods. Exercise thermal lag, zero tolerance, power bounds, failed telemetry, write confirmation, and recovery when relevant. Use fake backends and controlled clocks; automated tests must not change real GPUs. No numeric coverage threshold is configured.

## Commit and Pull Request Guidelines

Follow the existing Conventional Commits style with English, lowercase imperative subjects, such as `fix: handle missing temperature readings`. Keep commits focused. PRs should explain behavior changes, reference relevant issues, list validation commands/results, and describe deployment impact. Update affected examples and documentation.

## Configuration and Deployment

Keep local `config.json` ignored. Prefer UUIDs and driver-reported power ranges. Preserve settling after confirmed writes and critical-temperature protection. Current rig profiles use 62 °C, zero tolerance, and 60-second settling. Deployment uses `/opt/rigtune`, `/etc/rigtune/config.json`, and `rigtune.service`; hardware writes require root. Preserve active configuration when updating installations.
