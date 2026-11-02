# Security Policy

## Reporting a vulnerability

Please open a private security advisory on GitHub (Security → Advisories → "Report a
vulnerability") rather than a public issue. You should get an acknowledgement within a week.

## Scope notes

- The agent never executes code or shells out; tools are pure Python over a local SQLite DB.
- `GOOGLE_API_KEY` is read from the environment / `.env` only and is never logged.
- Tool arguments are validated before execution and tool outputs are scrubbed for prompt
  injection before being shown to the model (`veggie_planner/agents/callbacks.py`).
- The REST API has **no authentication** by design (demo project). Put it behind an
  authenticating proxy (IAP, API gateway) before exposing it beyond localhost.
