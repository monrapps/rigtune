# Changelog

Versions follow `YYYY.MM.DD[.N]`, using the release date in America/Sao_Paulo
and an optional revision for additional releases on the same day.

## 2026.10.13 — 2026-10-13

Initial stable release from `main`.

- Control each NVIDIA GPU's power limit on Linux and HiveOS using core
  temperature, targeting maximum hashrate within configured thermal limits.
- Account for thermal lag with confirmed writes, settling time, warmup,
  temperature smoothing, and trend checks. Use smaller adjustments near target.
- Support per-GPU settings, UUID selection, driver-validated power ranges,
  configurable tolerance including zero, and read-only dry runs.
- Protect against critical temperatures, unavailable sensors, failed writes,
  and competing controller instances, with independent recovery for each GPU.
- Include a systemd service and Hades/T37 examples using a 62 °C target,
  zero tolerance, 60-second settling, and full hardware power ranges.
- Provide English documentation, messages, and a hardware-free `unittest` suite.

Requires Python 3.6+, NVIDIA drivers with `nvidia-smi`, and root privileges
to apply power limits. No external Python packages or HiveOS API token are
required. HiveOS dashboard synchronization remains on the experimental
`feat/hiveos-sync` branch and is not part of this release.

When updating an existing installation, preserve `/etc/rigtune/config.json`.
Stopping the controller keeps the last applied power limit. Restarting begins
a new warmup and observation period.
