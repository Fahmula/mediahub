# MediaHub

A self-hosted, mobile-first dashboard for keeping an eye on **Emby**, **Sonarr**, **Radarr** and
**InfiniDysk**: what's playing, what's downloading, and what needs attention. Each install
monitors its own services; trusted installs can be linked read-only to see each other.

Built with FastAPI, Jinja2 + HTMX, Tailwind CSS + daisyUI, SQLite. One small container, no
Redis, no Node.js at runtime.

## Features

- **Dashboard:** now playing first, then Needs Attention, downloads, InfiniDysk and your servers.
  Built for phones, with a bottom nav and live counts.
- **Notifications:** an alert on your phone when something breaks, even with the app closed, and a
  follow-up when it clears.
- **Emby:** live sessions with posters, progress and transcode details, in a detailed or compact
  layout.
- **Sonarr and Radarr:** the download queue and recent history, with season packs grouped into one
  card and import warnings told apart from real blocks.
- **InfiniDysk:** provider speed, errors, connections and circuits, cache metrics and the reads in
  progress.
- **Stream health:** each Emby stream is matched to its InfiniDysk read, so you see whether it was
  served cleanly, recovered or failed, in InfiniDysk's own words.
- **Next-episode health check:** optionally has InfiniDysk check the next episode while you watch,
  so a broken release can be repaired before you reach it.
- **Play history:** 30 days of plays matched to InfiniDysk, with any problems they had.
- **Needs Attention:** services that are down, health warnings, stuck imports, failed downloads
  nothing retried, and provider circuits or limits. Acknowledge an issue or mute it for an hour,
  a day or a week.
- **Linked installs:** see another trusted MediaHub read-only, alongside your own.
- **Installable:** add it to a phone or desktop as an app.

## Quick start

### Docker Compose

```bash
docker compose up -d --build
```

That builds from this checkout. Release images are published to `ghcr.io/fahmula/mediahub`; to use
one, remove `build: .` from `docker-compose.yml`.

Open `http://<host>:8080`, create the admin account, then **Servers → Add service**.

Want to see the UI first? Set `MEDIAHUB_DEMO_MODE=true` to show generated data, including a
simulated second server.

### Unraid

See [docs/unraid.md](docs/unraid.md). A template is in
[unraid/my-mediahub.xml](unraid/my-mediahub.xml).

### Install as an app

Served over HTTPS, MediaHub can be installed from the browser (**Install app** /
**Add to Home Screen**) and opens like a native app, with an offline page when the server is
unreachable. The service worker only caches static assets and that page, never monitoring
data; it shows notifications as they arrive but doesn't keep them. Over plain `http://` it works
as a normal website.

### Notifications

Turn them on per device under **Settings → Notifications**, and use **Send a test** to check that
one arrives. They need HTTPS. On iPhone and iPad they also need iOS 16.4 or later, with MediaHub
added to the Home Screen and opened from there.

A device is told about **errors** (a service down, a failed download, a linked server offline) and
when they clear, including errors on linked servers. Warnings stay in Needs Attention. To avoid
false alarms a problem has to last about 2 minutes before it alerts and stay gone for 5 before
it's called resolved, and each problem alerts once. Problems that started before a device was
turned on, and anything you've acknowledged or muted, don't alert.

Delivery goes through your browser's push service (Apple, Google, Mozilla or Microsoft), so
MediaHub needs outbound HTTPS. Messages are end-to-end encrypted, with file paths removed first:
the push service sees when a message was sent and its rough size, not what it says. The text can
appear on your lock screen, and that includes download titles. Signing out turns notifications off
for that device; a login that simply expires doesn't.

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

### Play history

**Activity → Play history** lists each play MediaHub could match to InfiniDysk over the last 30
days, opening on the ones with problems, with InfiniDysk's own messages. It needs InfiniDysk's
backend URL and internal key (the live feed). It shows only what InfiniDysk logged, so a stutter
it didn't log looks clean, and each install records its own plays: history isn't sent to linked
servers.

### Next-episode health check

Off by default. To turn it on, edit an Emby service under **Servers** and enable **Health-check
the next episode**; **Check the next episode at (% played)** sets when, 50% if left blank. MediaHub
then asks every InfiniDysk on the same server to recheck the next episode once per playing item,
and the stream card follows the result. A file is asked for at most once in 24 hours.

## Configuration

Everything except the environment below lives in the web UI and the `/config` volume.

| Variable | Default | Purpose |
|---|---|---|
| `MEDIAHUB_HOST` | `0.0.0.0` | Bind address |
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
| `MEDIAHUB_PUSH_CONTACT` | project URL | Contact (URL or `mailto:`) sent to browser push services with each notification |
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
  URLs. Notification addresses come from the browser, so only known browser push services are
  accepted, and they're encrypted at rest. InfiniDysk calls are restricted to a read-only allowlist (some of its GET routes change
  state). The one write is the optional next-episode health check, which sends InfiniDysk's own
  recheck request.
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
uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest
```

New database migrations:

```bash
MEDIAHUB_CONFIG_DIR=./.dev-config uv run alembic revision --autogenerate -m "describe change"
```

### Commits

Commits on `main` follow [Conventional Commits](https://www.conventionalcommits.org/), because
they decide the next version and the changelog. PRs are squash-merged, so the PR title is what
counts, and a check on each PR enforces it.

| Commit | Release |
|---|---|
| `feat:` | Minor |
| `fix:`, `perf:`, `deps:`, `revert:` | Patch |
| `feat!:`, `fix!:` or a `BREAKING CHANGE:` footer | Minor until 1.0, then major |
| `docs:`, `refactor:`, `test:`, `build:`, `ci:`, `chore:`, `style:` | None, and left out of the changelog |

An optional scope is fine, e.g. `fix(emby): handle missing poster`.

### Releasing

Releases use [release-please](https://github.com/googleapis/release-please). Once `main` has a
commit that releases, a release PR stays open with the next version and changelog, updated on each
merge; no image is built yet. To publish, merge that PR. It tags `vX.Y.Z` and creates the GitHub
release, then CI runs and pushes `ghcr.io/fahmula/mediahub` tagged `X.Y.Z`, `X.Y` and `latest` (the
app reports the tag as its version).

- CI doesn't run on the release PR itself, because GitHub doesn't start workflows for PRs opened
  with `GITHUB_TOKEN`. It runs after the merge and must pass before the image is built.
- To choose the version, add a `Release-As: 1.0.0` footer to a commit.
- Pushing a `v*` tag by hand still builds an image; pre-release tags such as `v1.0.0-rc1` don't
  move `latest`.

### Layout

```
src/mediahub/
  integrations/   one package per service; raw API → normalized models (models/)
  monitoring/     poller (one task per target), demand tracker, in-memory snapshot store, issues
  aggregation/    view models for "all servers" / "this server" / one peer
  web/            routes, templates helpers; templates/ and static/ alongside
  history/        play history storage
  push/           Web Push: encryption and signing, delivery with retries, subscriptions
  db/ security/ instances/ peers/
```

MediaHub must run as **one process** per config directory: the poller and cache are in-process,
and a lock file (`/config/mediahub.lock`) refuses a second instance.
