# Email Verifier

Free, self-hosted email list verifier. Six stages; an address that fails a stage stops there.

| # | Stage | Result |
|---|-------|--------|
| 1 | Clean: trim, lowercase, strip `mailto:` / `Name <...>`, fix common domain typos (`gmial.com -> gmail.com`, `.con -> .com`), de-duplicate. The original value is always kept | duplicates removed, `typo_fixed` filled |
| 2 | Syntax (RFC length limits, characters, labels, TLD) | Invalid |
| 3 | Disposable domains (open-source GitHub list, 10k+ domains) | Invalid |
| 4 | Role addresses (`info@`, `sales@`, `admin@`...) are **flagged, never removed** | Valid role address becomes Risky |
| 5 | MX lookup, once per unique domain, cached 7 days. NXDOMAIN / null MX / no mail server | Invalid. DNS timeout gives Risky |
| 6 | SMTP `RCPT TO` probe, no `DATA` (nothing is ever sent). Catch-all test per domain first | Valid / Invalid / Risky |

Stage 6 rules:
- **Gmail, Outlook/Hotmail/Live, Yahoo** are never probed (Risky: *Major provider, not SMTP-verifiable*).
  Domains **hosted** on Google Workspace / Microsoft 365 are skipped too (`SKIP_HOSTED_BUSINESS=true`).
- Per-provider rate limit + random jitter, max 10 recipients per connection, 4 parallel workers.
- `4xx` (greylisting, temporary errors) and timeouts are **Unknown**, never Invalid. They are retried after 10 minutes, up to 2 times, then marked Risky.
- If a server rejects the probe IP itself (Spamhaus, "blocked", etc.), the result is *Unknown: server blocked the probe IP*.
  If 50% of the last 20 servers do that, stage 6 switches off for the rest of the run.
- If outbound port 25 is closed, stages 1-5 run normally and every address that would need stage 6 goes to Risky with
  *Not SMTP-checked: outbound port 25 is blocked*.

## Setup (macOS)

```bash
cd ~/email-verifier
./scripts/setup.sh
```

## Dashboard

```bash
./scripts/start_dashboard.sh
# open http://127.0.0.1:8765
```

Upload CSV/XLSX, start / pause / resume, live counters, Excel + CSV downloads, single-address check.

## Terminal

```bash
source .venv/bin/activate
python -m verifier smtp-test                      # port 25 + public IP + HELO name
python -m verifier run ~/Downloads/list.csv       # verify + export (Ctrl+C pauses safely)
python -m verifier run list.xlsx --column "Email" # force the email column
python -m verifier resume 3                       # continue job #3
python -m verifier jobs
python -m verifier export 3
python -m verifier check someone@company.ae
python -m verifier update-lists                   # refresh the disposable-domain list
```

Running `run` on the same file again resumes the unfinished job automatically.

## Outputs (`runtime/jobs/job_<id>/`)

- `<name>_verified.xlsx`: **Summary** (counts and % per category, breakdown by reason, top domains with valid %),
  **Valid**, **Risky**, **Invalid**. Columns: `email, original_email, status, reason, domain, mx_host, is_role,
  is_disposable, typo_fixed, smtp_response` + all original columns.
- `<name>_valid.csv`: Valid only (`email, domain` + original columns), ready for n8n.

## API (for n8n)

```
POST http://127.0.0.1:8765/api/verify     {"email": "lead@company.ae"}
GET  http://127.0.0.1:8765/api/verify?email=lead@company.ae
-> {"email","status":"Valid|Risky|Invalid","reason","domain","mx_host","is_role","is_disposable","typo_fixed"}
```

Other endpoints: `GET /api/health`, `GET/POST /api/jobs`, `GET /api/jobs/{id}`,
`POST /api/jobs/{id}/start|pause`, `GET /api/jobs/{id}/download/xlsx|csv`, `DELETE /api/jobs/{id}`.

The server listens on `127.0.0.1` only. An n8n instance on a VPS cannot reach it until you expose it
(e.g. Cloudflare Tunnel / Tailscale). Add authentication before you do that.

## Configuration

Copy `.env.example` to `.env` (setup does this) and edit. The important ones:

| Key | Default | Note |
|-----|---------|------|
| `PROBE_HELO` | auto | reverse DNS of your IP, else the PROBE_FROM domain |
| `PROBE_FROM` | `verify@mahmoudyassin.online` | must be a real domain (servers reject `<>`). **Never use a sending domain** |
| `PER_MX_INTERVAL` / `PER_MX_JITTER` | 8 / 7 s | gap between connections to one provider |
| `SMTP_WORKERS` | 4 | parallel domains |
| `RETRY_DELAY` / `MAX_RETRIES` | 600 s / 2 | greylisting |
| `SKIP_HOSTED_BUSINESS` | true | set false to probe Google Workspace / M365 business domains |
| `SMTP_ENABLED` | true | false = stages 1-5 only |

## Architecture

```
verifier/
  cleaning.py      stages 1-4 (pure functions)
  dns_check.py     stage 5
  smtp_check.py    stage 6 + rate limiter
  engine.py        pipeline, resume, retries, circuit breaker, single-address API
  storage/         Storage interface + SQLite implementation (+ Supabase schema)
  export.py        Excel + CSV
  cli.py           terminal
server/            FastAPI + static dashboard
tests/             mock SMTP server + end-to-end tests (pip install -r requirements-dev.txt)
```

To move to Supabase, implement `verifier/storage/base.py` in a new class and return it from
`verifier/storage/__init__.py:open_storage()`. Nothing else changes.

## Tests

```bash
pip install -r requirements-dev.txt
python tests/run_mock_test.py     # all stages against a fake mail server
python tests/run_resume_test.py   # pause/resume + port-25-blocked fallback
```
