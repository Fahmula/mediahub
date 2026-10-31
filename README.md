# MediaHub

A self-hosted, mobile-first dashboard for keeping an eye on **Emby**, **Sonarr**, **Radarr** and
**InfiniDysk**: what's playing, what's downloading, and what needs attention. Each install
monitors its own services; trusted installs can be linked read-only to see each other.

Built with FastAPI, Jinja2 + HTMX, Tailwind CSS + daisyUI, SQLite. One small container, no
Redis, no Node.js at runtime.

> **Status: Phase 5.** Emby live sessions, Sonarr/Radarr queues and history, InfiniDysk
> streaming, provider and cache metrics with live reads, a Needs Attention list you can
> acknowledge or mute, and read-only linking of MediaHub installs.

## Quick start

### Docker Compose

```bash
docker compose up -d --build
```

Open `http://<host>:8080`, create the admin account, then **Servers → Add service**.

Want to see the UI first? Set `MEDIAHUB_DEMO_MODE=true` to show generated data, including a
simulated second server.

### Unraid

See [docs/unraid.md](docs/unraid.md). A template is in [unraid/mediahub.xml](unraid/mediahub.xml).

### Install as an app

Served over HTTPS, MediaHub can be installed from the browser (**Install app** /
**Add to Home Screen**) and opens like a native app, with an offline page when the server is
unreachable. The service worker only caches static assets and that page, never monitoring
data. Over plain `http://` it works as a normal website.

### Link two installs

Each MediaHub can show another one's activity, read-only. On the server to be read, open
**Servers → Access tokens**, create a token (shown once) and copy it. On
the other server, **Link a server** with the first one's URL and that token. To see each other,
do it in both directions.

- Use a private network: ideally the other server's Tailscale HTTPS name (`tailscale serve`).
  Plain `http://` outside Tailscale or a LAN is flagged, since the token could be intercepted.
- A linked server sees everything you see except credentials, client IP addresses and file
  paths (removed before data leaves your server). It only ever sees your own services, never
  the servers you link to.
- Revoke a token at any time; that server loses access immediately.

## Configuration

Everything except the environment below lives in the web UI and the `/config` volume.

| Variable | Default | Purpose |
|---|---|---|
| `MEDIAHUB_PORT` | `8080` | Listening port |
| `MEDIAHUB_CONFIG_DIR` | `/config` | Database, encryption key, cache |
| `MEDIAHUB_SERVER_NAME` | `MediaHub` | Server name on first start (editable later) |
| `MEDIAHUB_COOKIE_SECURE` | `auto` | `auto` = secure cookie when served over HTTPS; `true` behind an HTTPS proxy |
| `MEDIAHUB_TRUSTED_PROXIES` | `127.0.0.1` | IPs allowed to set `X-Forwarded-For/Proto` |
| `MEDIAHUB_SESSION_DAYS` | `30` | Login session lifetime (sliding) |
| `MEDIAHUB_LOG_LEVEL` / `MEDIAHUB_LOG_FORMAT` | `INFO` / `json` | Logging (`console` for humans) |
| `MEDIAHUB_SECRET_KEY` | *(generated)* | Optional master key; otherwise `/config/secret.key` is created |
| `MEDIAHUB_ADMIN_USER` / `MEDIAHUB_ADMIN_PASSWORD` | – | Optional headless admin creation (only while no user exists) |
| `MEDIAHUB_DEMO_MODE` | `false` | Generated sample data |
| `PUID` / `PGID` / `UMASK` | `99` / `100` / `022` | Container user (Unraid defaults) |

**Back up `/config/mediahub.db` and `/config/secret.key` together.** Service credentials are
encrypted with a key derived from `secret.key`; without it they must be re-entered.

## Security model

- Single admin account per install (Argon2id). Server-side sessions; only a hash of the session
  token is stored. Login is rate limited.
- CSRF tokens on every state-changing request; strict Content-Security-Policy (no inline
  scripts); `X-Frame-Options: DENY`.
- Service API keys are encrypted at rest (Fernet), sent to services only in headers, never
  rendered back to the browser, and redacted from logs.
- Redirects from services are never followed; the browser can't make MediaHub fetch arbitrary
  URLs. InfiniDysk calls are restricted to a read-only allowlist (some of its GET routes change
  state).
- Linked servers authenticate with a bearer token (stored only as a SHA-256 hash, revocable,
  rate limited) on a separate read-only `/peer/v1` API without cookies. Client IPs and file
  paths are stripped from everything served there; a peer's own peers are never relayed.
- The container drops to `PUID:PGID`, needs no privileges and no Docker socket, and runs with a
  read-only root filesystem.

## Service notes

| Service | What you need |
|---|---|
| Emby | URL (e.g. `http://192.168.1.10:8096`) and an API key (Settings → Advanced → API Keys) |
| Sonarr / Radarr | URL including any URL base, and the API key (Settings → General) |
| InfiniDysk | Frontend URL (port 3000) + API key (Settings → SABnzbd). For health, cache metrics and live streams also the **backend URL** (port 8080, same Docker network) and the **internal key** (`FRONTEND_BACKEND_API_KEY`, pinned in InfiniDysk's container settings) |

## Development

Requires [uv](https://docs.astral.sh/uv/). No Node.js: CSS is built with the Tailwind
standalone CLI, downloaded and checksum-verified into `./bin`.

```bash
uv sync
```

```bash
scripts/build-css.sh --watch
```

```bash
MEDIAHUB_DEMO_MODE=true MEDIAHUB_CONFIG_DIR=./.dev-config MEDIAHUB_LOG_FORMAT=console uv run python -m mediahub
```

```bash
uv run ruff format . && uv run ruff check . && uv run pytest
```

New database migrations:

```bash
MEDIAHUB_CONFIG_DIR=./.dev-config uv run alembic revision --autogenerate -m "describe change"
```

### Releasing

Merging to `main` runs CI only; no image is built. To publish one, run **Actions → Release**
on `main` and choose `patch`, `minor` or `major`. It runs CI, bumps the latest `vX.Y.Z` tag,
creates a GitHub release with generated notes, and pushes `ghcr.io/fahmula/mediahub` tagged
`X.Y.Z`, `X.Y` and `latest` (the app reports the tag as its version). Pushing a `v*` tag by
hand builds the image too; pre-release tags such as `v1.0.0-rc1` don't move `latest`.

### Layout

```
src/mediahub/
  integrations/   one package per service; raw API → normalized models (models/)
  monitoring/     poller (one task per target), demand tracker, in-memory snapshot store, issues
  aggregation/    view models for "all servers" / "this server" / one peer
  web/            routes, templates helpers; templates/ and static/ alongside
  db/ security/ instances/ peers/
```

MediaHub must run as **one process** per config directory: the poller and cache are in-process,
and a lock file (`/config/mediahub.lock`) refuses a second instance.
