# Changelog

## 0.1.1

- Read recent Home Assistant warnings and errors on Home Assistant OS again:
  the log-file endpoint returns 404 there, so the assistant now falls back to
  the in-memory system log over the read-only websocket API.
- Document the **Show in sidebar** toggle used by Supervisor 2026.11 and later,
  and keep the panel automatic on older Supervisors.
- Conversations survive an App restart: they are kept only in the App's local
  data folder, pruned after 7 days, and cleared by **New chat**.
- The monthly allowance message now names the reset date in UTC.

## 0.1.0

- First release.
- Read-only AI chat panel for Cytech Comfort systems, available in the Home
  Assistant sidebar.
- Reads live Home Assistant entities, recent history, and error-log lines; never
  writes.
- Explains Comfort status using the raw status sensors, including the
  `Trouble`/`Alert` versus `disarmed` alarm-panel mapping.
- Sonnet 5.5 (default) and Haiku 4.5 models, using Cytech's hosted AI service.
- Monthly allowance display, friendly errors, and redaction of credentials in
  logs and entity attributes.
