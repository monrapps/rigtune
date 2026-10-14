# RigTune

Controls the **power limit of each NVIDIA GPU based on temperature** on
Linux/HiveOS. Requires Python 3.6+ and `nvidia-smi`, with no external packages.
Power is configured in watts and temperature in °C.

The objective is to **maximize hashrate within the thermal target**, making use
of the available energy. The controller increases power when there is thermal
headroom and reduces it as the environment warms up, such as during the
afternoon. Control is based on GPU core temperature and respects each card's
power limits. Reaching the exact target also depends on workload, ventilation,
and ambient temperature. The miner tracks hashrate; RigTune controls the power
limit (PL) using temperature.

## Quick Start

Clone the project and run commands from its directory:

```bash
git clone https://github.com/monrapps/rigtune.git
cd rigtune
python3 -m rigtune list
cp config.example.json config.json
nano config.json
python3 -m rigtune check --config config.json --hardware
python3 -m rigtune run --config config.json --dry-run
```

After reviewing the decisions, start applying adjustments:

```bash
sudo python3 -m rigtune run --config config.json
```

`Ctrl+C` stops the controller. The last applied PL remains in effect; stopping
does not restore a previous, potentially higher limit. `--once` runs a single
cycle while preserving warmup and settling rules. `check` without `--hardware`
validates the file without querying GPUs.

The `--dry-run` mode reads real sensors and maintains a virtual PL between
cycles, but **does not reproduce the thermal response to unapplied adjustments**.
The test suite includes a simulated thermal model for testing convergence with
thermal lag.

## Configuration

[config.example.json](config.example.json) contains all options. Its values are
examples; check each GPU model's range with `list`.

```json
{
  "poll_interval_s": 5,
  "command_timeout_s": 10,
  "defaults": {
    "power_min_w": 100,
    "power_max_w": 160,
    "target_temp_c": 60,
    "hysteresis_c": 2,
    "critical_temp_c": 75,
    "step_up_w": 3,
    "step_down_w": 5,
    "fine_step_w": 1,
    "fine_band_c": 5,
    "settle_time_s": 60
  },
  "gpus": [
    { "id": 0 },
    { "id": 1, "power_min_w": 125, "power_max_w": 185, "target_temp_c": 62 }
  ]
}
```

`gpus` explicitly lists the cards to control. `id` accepts an **nvidia-smi**
index or the full UUID returned by `list`. Prefer UUIDs: indices can change
after a reboot and may differ from the miner/HiveOS order. At startup, indices
are resolved to UUIDs; all writes use those UUIDs. Partial selectors, duplicate
GPUs, and unknown field names are rejected.

Any field in `defaults` can be overridden within a `gpus` entry. `power_min_w`,
`power_max_w`, and `target_temp_c` are required, either per GPU or in `defaults`.
The configured range must fit entirely within the driver-reported limits;
all GPUs are validated before any changes are made.

| Field | Default | Effect |
| --- | --- | --- |
| `poll_interval_s` | 5 | Interval between readings of all GPUs. |
| `command_timeout_s` | 10 | Timeout for each `nvidia-smi` call. |
| `power_min_w` / `power_max_w` | required | Lowest and highest allowed PL, in whole watts. |
| `target_temp_c` | required | Desired core temperature. |
| `hysteresis_c` | 2 | Tolerance around the target, e.g. 60 ± 2 °C. Set to 0 to remove the tolerance band. |
| `critical_temp_c` | 75 | Raw temperature that triggers a reduction to minimum power. |
| `step_up_w` | 3 | Increase step far from the target. |
| `step_down_w` | 5 | Decrease step far from the target. |
| `fine_step_w` | 1 | Step used near the target in either direction. |
| `fine_band_c` | 5 | Distance from the target within which the fine step applies. |
| `settle_time_s` | 60 | Minimum wait after a confirmed adjustment. |
| `warmup_s` | 60 | Continuous time under load before increases are allowed. |
| `trend_window_s` | 30 | Reading window used to measure the temperature trend. |
| `max_rise_c_per_min` | 0.5 | Blocks increases when temperature rises faster than this. |
| `temperature_alpha` | 0.3 | Weight of the latest reading in the exponential average; 1 disables smoothing. |
| `min_utilization_pct` | 30 | Minimum load for increasing PL; uses the higher GPU or memory utilization. Set to 0 to disable this filter. |
| `sensor_failure_limit` | 3 | Consecutive temperature/PL failures before attempting minimum power. |
| `recovery_s` | 60 | Time with valid readings and temperature at or below the band's upper bound before leaving protection. |

Steps must be positive integers; `fine_step_w` must not exceed the normal
steps. The fine band must exceed the tolerance. Critical temperature must
exceed `target_temp_c + hysteresis_c`. Settling, trend, and recovery windows
must last at least one polling interval. JSON changes take effect when the
program restarts.

## Thermal Lag and Adjustment Steps

The controller makes one adjustment and waits to observe its effect:

1. Read temperature, utilization, power draw, and current PL. Smoothing uses an
   exponential average; decisions use the higher of raw and smoothed temperature
   so that averaging does not hide a recent temperature rise.
2. Hold PL within the tolerance band. Above the band, decrease it; below the
   band, an increase is allowed if there is load and warmup has finished.
3. Use smaller steps near the target. With a target of 60, tolerance of 2, and
   fine band of 5: increase by 1 W at 56 °C or 3 W at 50 °C; decrease by 1 W at
   64 °C or 5 W at 70 °C.
4. Confirm the new PL with another driver reading. **Settling starts after this
   confirmation**, and the trend history is reset.
5. Continue monitoring during `settle_time_s`, waiting before another normal
   adjustment. Afterward, allow an increase only with a full observation window
   and a temperature rise rate at or below the limit.

The trend uses linear regression of raw temperatures against monotonic time.
A slow rise below the limit still permits an increase; this rule prevents
stacking steps while the card is responding rapidly to the previous one.
Normal decreases also respect settling. Critical temperature and sensor-failure
protection can act immediately, as can correcting a PL above the configured
maximum.

Start with the example timings and small steps, and observe the curves on the
rig. If the response takes longer, increase `settle_time_s` and `trend_window_s`;
if there is oscillation, reduce the steps. Rapidly changing fan speeds affect
the response and make it harder to attribute temperature changes to PL.

If the initial PL is above the maximum, the program reduces it directly to the
maximum. If it is below the minimum, it is raised to the minimum only when the
conditions for an increase are met. These initial range corrections and
protective reductions are exceptions to the step sizes. A hot GPU with PL below
the minimum never receives an increase just to bring it into range.

## Protections and Limitations

- Critical temperature: attempt `power_min_w` immediately, without waiting for
  smoothing or settling. Stay in protection until `recovery_s` has elapsed with
  valid, cool readings; then require warmup under load again.
- Missing, `N/A`, invalid, or timed-out readings: block increases from the first
  failure; at the failure limit, attempt minimum power using the known UUID.
  Protection is independent for each GPU.
- Failed write or readback that differs from the requested PL: log an error and
  enter protection, attempting minimum power on the next cycle.
- Idle or unknown utilization: do not increase PL. Heat-driven decreases remain
  available. If one utilization channel is available, use it.
- Unreachable target: stay at the range boundary. `hot_at_minimum` indicates that
  the card remains hot at the lowest configured PL; `cold_at_maximum` indicates
  the opposite.
- Detect external PL changes and restart settling. UUID locks in
  `/run/lock/rigtune` prevent two active instances from controlling the same GPU.

The configured minimum is also the protection PL, so choose it with the GPU
model and workload in mind. The program controls **core temperature**, not
memory/hotspot temperature, fans, or clocks. It does not stop the miner. If the
driver stops accepting commands, power reduction cannot be guaranteed; retain
the driver's thermal protections and HiveOS shutdown mechanisms.

## HiveOS and systemd

The controller runs independently of HiveOS. To also update the power limit
shown in its web interface, use the [optional HiveOS synchronization service](docs/hiveos.md).
It runs separately, needs an API token, and is disabled unless installed and
started explicitly. API failures do not block thermal control.

HiveOS uses absolute watts for PL. Overclock profiles, algorithm changes, and
other programs may reapply limits. Avoid another periodic PL controller
competing for the same GPU; external changes appear in the log. AutoFan controls
ventilation and also affects temperature: configure it consistently with
RigTune's thermal target and observe how the two interact.

To install, run the following on the rig from the project directory:

```bash
sudo install -d /opt/rigtune /etc/rigtune
sudo cp -r rigtune /opt/rigtune/
sudo install -m 0644 config.example.json /etc/rigtune/config.json
sudo nano /etc/rigtune/config.json
cd /opt/rigtune
sudo python3 -m rigtune check --config /etc/rigtune/config.json --hardware
sudo python3 -m rigtune run --config /etc/rigtune/config.json --dry-run
```

Stop the dry run, return to the original project directory, then install and
enable the service:

```bash
sudo install -m 0644 deploy/rigtune.service /etc/systemd/system/rigtune.service
sudo systemctl daemon-reload
sudo systemctl enable --now rigtune
sudo journalctl -u rigtune -f
```

Preserve `/etc/rigtune/config.json` during updates. Apply configuration changes
with `sudo systemctl restart rigtune`; stop and disable the service with
`sudo systemctl disable --now rigtune`. The service restarts after runtime
failures; configuration errors (exit code 2) require a fix before a manual
restart.

Logs show UUID, raw/smoothed temperature, trend, power draw, PL, and the decision
reason. Stable states produce one record per minute; transitions and adjustments
are logged immediately. Per-GPU failures keep the loop running to allow recovery.
With `--once`, exit code 1 indicates a failure, protection, or heat at minimum
power; 0 indicates a cycle without those states.

## Tests

```bash
python3 -m unittest discover -v
```

Tests use simulated sensors and commands, including a thermal model with a
90-second time constant. They cover steps, tolerance, settling after
confirmation, trends, load, limits, driver/sensor failures, recovery, UUID
selection after reordering, interprocess locking, and dry runs without writes.
No test in this suite changes real GPUs.

### Hades Validation

On October 13, 2026, the then-current 43 tests passed locally on Python 3.13
and on Hades with Python 3.10.12, Ubuntu 22.04.5, and NVIDIA driver 580.173.02.
The systemd unit passed `systemd-analyze verify`. Python 3.6 syntax compatibility
was checked; execution on that older version has not yet been tested.

With authorization to test the rig, a 66-second trial ran on the RTX 3060:
**175 → 174 → 175 W**, confirming the write, 60-second settling, and restoration.
The GTX 1080 Ti remained at 185 W. The trial range was restricted to 174–175 W
with an artificial 50 °C target; `hot_at_minimum` after settling was expected
because the card stayed at 58 °C. This trial validates hardware integration;
thermal convergence was validated with the simulated model.

The [test record](reports/hades-hardware-smoke.json) contains readings and
confirmations. See [report notes](reports/README.md) for the provenance of
translated historical logs. [config.hades.example.json](config.hades.example.json)
uses the actual UUIDs and was updated to the requested operating configuration:
a **62 °C** target, **0 °C** tolerance, and the full driver-reported ranges:
**125–300 W for the GTX 1080 Ti** and **100–212 W for the RTX 3060**. The controller
gradually increases or decreases PL within these ranges; it does not apply the
maximums directly. Relevant performance metrics are hashrate, temperature, and
share stability.

The trial copy was left at `/tmp/rigtune-test.BpVI6I` on Hades. After the trial
ended and its original limits were restored, continuous operation was installed
at `/opt/rigtune`, with configuration at `/etc/rigtune/config.json` and
`rigtune.service` enabled at boot. To monitor operation:

```bash
ssh root@Hades 'systemctl status rigtune --no-pager'
ssh root@Hades 'journalctl -u rigtune -f'
```

### T37 Operation

T37 also runs `rigtune.service`, enabled at boot, with **five GTX 1080 Ti GPUs**.
Its configuration uses a **62 °C target**, **0 °C tolerance**, **60-second
settling**, and the hardware range of **125–300 W per GPU**. Cards are selected
by UUID in [config.t37.example.json](config.t37.example.json).

The program is installed at `/opt/rigtune`, and the active configuration is at
`/etc/rigtune/config.json` on T37. To monitor it:

```bash
ssh root@T37 'systemctl status rigtune --no-pager'
ssh root@T37 'journalctl -u rigtune -f'
```

## References

- [NVIDIA SMI](https://docs.nvidia.com/deploy/nvidia-smi/index.html): sensor
  queries, stable UUID identification, and power limits within hardware bounds.
- [HiveOS Overclocking](https://hiveon.com/knowledge-base/getting_started/start_oc/):
  PL in watts and profile configuration.
- [HiveOS AutoFan](https://hiveon.com/knowledge-base/guides/autofan/):
  temperature-based fan control.
