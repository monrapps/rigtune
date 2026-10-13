# Validation Reports

These JSON files record hardware validation and service observations from
October 13, 2026:

- `hades-hardware-smoke.json`: a controlled 1 W adjustment, settling, and
  restoration test on Hades.
- `hades-service-status.json` and `t37-service-status.json`: service snapshots,
  configurations, readings, and journal excerpts.
- `hades-audit-20261013.json` and `t37-audit-20261013.json`: longer observation
  windows with telemetry, service state, and journal records.

## Translation and Provenance

Human-readable text in the four service/audit reports' `journal` fields has
been translated from Portuguese into English. These excerpts are translated
records, not verbatim copies of the original logs. Only message wording and
display labels changed; timestamps, measurements, GPU identities, configuration
values, state identifiers, JSON keys, and record order are preserved.

The original reports remain available in
[commit b550f93](https://github.com/monrapps/rigtune/tree/b550f93dc4cd0f199f13d22de786e6f19241d4ef/reports).
The hardware smoke-test report was already in English and remains unchanged.

Reports describe the configuration and behavior at collection time. Earlier
snapshots may differ from the current example profiles; they are historical
evidence, not live rig status.
