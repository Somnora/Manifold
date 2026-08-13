# Team mode: one Manifold, several people

One machine runs the backend; teammates reach it over the network with
their own named tokens. The guards, the audit trail, and the spend ledger
are shared, which is the point: one place where the money is visible and
one set of rules it obeys.

## The two walls

The backend refuses to serve a network it is not ready for, judged on
every request by the interface the connection actually arrived on:

1. **No token, no network.** Requests arriving on a non-loopback
   interface are refused (403) while no `MANIFOLD_API_TOKEN` is
   configured. A launch console must never be an open LAN service.
2. **No plaintext tokens on the wire.** With a token configured,
   non-loopback plain-HTTP requests are refused unless
   `server.allow_plaintext_lan: true` in `config.yaml`. Set that ONLY
   when the wire is already encrypted below HTTP; a Tailscale/WireGuard
   tailnet is the canonical case. TLS needs no opt-in.

Local requests on 127.0.0.1 are never affected by either wall.

## Recommended: serve over Tailscale

The backend host joins your tailnet, and the wire is encrypted end to end
without certificates:

```bash
# on the backend machine, from backend/
uv run uvicorn app.main:create_default_app --factory --host 0.0.0.0
# config.yaml: server.allow_plaintext_lan: true  (the tailnet IS the TLS)
```

Teammates open `http://<tailscale-ip>:8000`, paste their token once, and
the dashboard is theirs. Prefer binding to the tailscale interface's own
IP over `0.0.0.0` when the machine also sits on an untrusted LAN.

## Alternative: TLS directly

```bash
uv run uvicorn app.main:create_default_app --factory \
    --host 0.0.0.0 --ssl-keyfile key.pem --ssl-certfile cert.pem
```

No config opt-in needed; the plaintext wall only watches `http`.

## Tokens: one per person, one per agent

On the Settings page (API access), mint a principal per teammate and per
agent, each with a role and, where it makes sense, an hourly ceiling:

- **role** - `viewer` observes (spend, instances, logs; launches
  nothing), `operator` works (launches, jobs, terminals, files),
  `admin` governs (secrets, policy, credentials). Admin tokens can only
  be minted by the owner token (the one in `.env`).
- **$/hr cap** - an ENFORCED ceiling on that principal's attributed
  hourly burn. A launch that would cross it is refused with the numbers
  in the message. Jobs and capacity watches count against whoever
  created them, so an auto-managed job cannot outspend its author.

Every launch, job, and audit row carries the principal's name, and the
Activity page's "Where it went" groups spend **by principal**.

## What stays local-only

- The **Hub page local terminal** is a shell on the BACKEND machine. Its
  origin allowlist already blocks remote browsers, but on a shared host
  set `hub.local_terminal: false` in `config.yaml` and remove the
  endpoint entirely.
- **Settings that write secrets** (`.env` writers) are admin-only; the
  `.env` itself never leaves the backend machine.
- **Mock mode** stays a zero-credential local demo; the no-token wall
  applies to it too, so a demo backend does not serve the LAN.

## The database stays SQLite (a decision, not a default)

Team mode is one shared backend process, not N backends sharing a
database. Behind one process, SQLite in WAL mode handles a small team's
write rate with room to spare, and every guard runs in-process where a
transaction is a function call. The `Database`/`TaskQueue` interfaces
are the swap point if a future phase genuinely needs a network database;
what would force that is multiple backend REPLICAS, which would also
need distributed guard state - a different product. See DECISIONS.md.
