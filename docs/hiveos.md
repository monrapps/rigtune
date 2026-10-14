# Optional HiveOS Synchronization

The thermal controller works independently of HiveOS. Its default command and
service need no network access, HiveOS installation, or API token. Integration
is an additional process, enabled by installing a separate service.

## What It Updates

HiveOS displays the power limit saved in the worker's overclock profile. Normal
agent telemetry reports power draw and temperature, which are different from
that saved limit. RigTune can synchronize the active profile with power limits
that its controller has confirmed through the NVIDIA driver.

The synchronizer sends a worker update using `oc_apply_mode: "merge"`. It changes
NVIDIA `power_limit` values, preserves uncontrolled GPUs, and follows the active
default or algorithm-specific profile. It matches controller UUIDs to PCI
addresses, then to the NVIDIA profile order; a CPU or AMD card in the HiveOS
inventory must not shift the NVIDIA power vector.
If limits for uncontrolled GPUs are unspecified in both the saved and resolved
profiles, synchronization is refused rather than resetting those GPUs to stock.

**This updates an operational profile, not just a display label.** HiveOS can
queue an overclock command and reapply the profile's existing clocks, memory,
and fan settings along with PL. The integration does not modify the HiveOS
agent or claim to suppress these commands. Review existing profiles and observe
one update before enabling continuous synchronization. Later algorithm changes
or reboots may also apply the saved profile.

## Configuration

Copy [hiveos.example.json](../hiveos.example.json) to
`/etc/rigtune/hiveos.json`. This file is separate from the controller's
`/etc/rigtune/config.json`.

| Setting | Default | Purpose |
| --- | --- | --- |
| `farm_id`, `worker_id` | required | Farm and worker to update. The worker ID must match the local HiveOS rig configuration. |
| `api_url` | `https://api2.hiveos.farm/api/v2` | HTTPS API endpoint. Redirects are rejected. |
| `token_file` | `/etc/rigtune/hiveos.token` | File containing only the API token. |
| `status_file` | `/run/rigtune/status.json` | Atomic snapshot written by the local controller. |
| `rig_config_file` | `/hive-config/rig.conf` | Used only to read and verify the numeric worker ID; never executed. |
| `sync_interval_s` | 60 | Minimum interval between synchronization attempts. |
| `retry_max_s` | 900 | Maximum retry delay after exponential backoff. |
| `request_timeout_s` | 10 | Timeout for each API request and driver query. |
| `max_status_age_s` | 20 | Maximum age of the controller snapshot. |
| `max_worker_age_s` | 120 | Maximum age of HiveOS telemetry before refusing an update. |

Use a personal API token from the HiveOS account that can manage the worker.
The farm must permit API access: HiveOS rejects API keys for free farms with
HTTP 403 (`Cannot use API keys to access free farms`). A valid token alone does
not remove that restriction. Verify access before enabling synchronization.
Store only its value, without quotes or a `Bearer` prefix, in `token_file`.
The file must belong to the service user and have mode `600` or `400`; symbolic
links are rejected. Tokens are read for each request so that replacing the file
does not require a restart. Keep tokens and local integration settings out of
Git. API error bodies and account responses are not written to logs.

## Install and Preview

First install or update the Python source at `/opt/rigtune` as described in the
[main README](../README.md#hiveos-and-systemd). Preserve the active controller
configuration. On an existing installation, back up the source before updating.

From the project directory, enable status snapshots with the optional drop-in:

```bash
sudo install -d /etc/systemd/system/rigtune.service.d
sudo install -m 0644 deploy/hiveos-status.conf /etc/systemd/system/rigtune.service.d/hiveos-status.conf
sudo systemctl daemon-reload
sudo systemctl restart rigtune
```

The drop-in adds `--status-file /run/rigtune/status.json`. Without systemd,
start the controller with:

```bash
sudo python3 -m rigtune run --config /etc/rigtune/config.json --status-file /run/rigtune/status.json
```

Use only one controller instance per GPU. A controller dry run never publishes
virtual power limits to the status file. The directory `/run` keeps frequently
updated snapshots off persistent storage on typical HiveOS installations.

Create the separate settings and token files:

```bash
sudo install -m 0600 hiveos.example.json /etc/rigtune/hiveos.json
sudo nano /etc/rigtune/hiveos.json
sudo install -m 0600 /dev/null /etc/rigtune/hiveos.token
sudo nano /etc/rigtune/hiveos.token
```

The token creation command is for a new installation; do not run it over an
existing token. Set the correct farm/worker IDs, then preview an update:

```bash
cd /opt/rigtune
sudo python3 -m rigtune hiveos-sync --config /etc/rigtune/hiveos.json --dry-run --once
```

This preview reads the API, the controller snapshot, and actual GPU limits. It
prints the proposed profile update without sending a PATCH or changing GPUs.
After reviewing it, perform one update and inspect the worker in HiveOS:

```bash
sudo python3 -m rigtune hiveos-sync --config /etc/rigtune/hiveos.json --once
```

An update is followed by an API readback. That confirms the saved cloud profile;
it does not prove that the HiveOS agent has already finished any queued command.
The synchronizer will not enqueue a different update while commands are pending.

## Continuous Operation

From the project directory:

```bash
sudo install -m 0644 deploy/rigtune-hiveos.service /etc/systemd/system/rigtune-hiveos.service
sudo systemctl daemon-reload
sudo systemctl enable --now rigtune-hiveos
sudo journalctl -u rigtune-hiveos -f
```

The controller and synchronizer have separate processes and locks. API failures,
authentication errors, rate limits, or slow requests cannot block temperature
control. Failed attempts retry with exponential backoff; every retry fetches
fresh state instead of replaying an old power vector.

Synchronization is skipped if the controller is unhealthy or recovering, its
snapshot is stale, telemetry is missing, an identity is ambiguous, the worker
is offline, or driver readings disagree with the snapshot. The controller keeps
its configured thermal protections. Cloud commands are asynchronous: a command
already accepted by HiveOS can still arrive after a local adjustment. The
controller detects external PL changes, and existing HiveOS/driver protections
must remain enabled.

To return to independent operation, disable the synchronization service:

```bash
sudo systemctl disable --now rigtune-hiveos
```

Status snapshots may remain enabled for monitoring. To remove them too, remove
only `hiveos-status.conf` from the drop-in directory, run
`sudo systemctl daemon-reload`, and restart `rigtune`. Disabling synchronization
does not restore earlier cloud profiles or cancel commands already queued by
HiveOS. The normal `rigtune.service` file remains usable on any supported Linux
system without HiveOS.

## Troubleshooting and Tests

- HTTP 401: check the full token value, its active status, and its account.
- HTTP 403: check worker permissions and API access restrictions. A free farm
  may reject API keys even when account authentication succeeds.
- Stale status: confirm the controller is active and uses `--status-file`.
- Pending commands: wait for the HiveOS agent to process its queue.
- GPU mismatch: verify the selected worker and inventory before restarting.

`--once` exits with `0` for a verified update, preview, or already matching
profile, and `1` for a skipped/failed attempt. Configuration errors exit with `2`.
Stopping the synchronization service leaves the thermal controller running.

Run `python3 -m unittest discover -v`. Tests cover both independent operation and
optional synchronization, including a local HTTP server, API failures, token
handling, stale snapshots, PCI/UUID mapping, profile preservation, readback,
retry delays, and dry runs without writes.

The [Hades local smoke test](../reports/hades-hiveos-local-smoke.json) exercised
real GPU reads with a fake API on the rig. It verified batching, dry run,
readback, unchanged profile fields, duplicate-update suppression, and an API
outage while the existing controller continued running. Live cloud writes and
their effect on the HiveOS agent remain untested: the farm rejected API keys,
and testing was deliberately limited to local execution.

## References

- [HiveOS API specification](https://app.swaggerhub.com/apis/HiveOS/public/2.1-beta):
  worker PATCH, `WorkerEditOCMode`, `OCConfig`, and `GpuInfo`.
- [HiveOS agent command handling](https://github.com/minershive/hiveos-linux/blob/master/hive/bin/agent.do_command.sh):
  applying changed NVIDIA overclock configurations.
