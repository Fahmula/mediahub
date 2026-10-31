# MediaHub

Self-hosted, mobile-first monitoring dashboard for Emby, Sonarr, Radarr and InfiniDysk.
FastAPI + Jinja2/HTMX + Tailwind v4/daisyUI 5 + SQLite (SQLAlchemy 2, Alembic). Python ≥3.12
(dev/Docker use 3.13). **Status, decisions and next steps: `PLAN.md`** (local-only, not in git;
listed in `.git/info/exclude`). Read it first when present, and update
its Status / Next up sections at the end of a work session.

## Commands

```bash
uv sync                                   # install deps (incl. dev group)
scripts/build-css.sh                      # build static/dist/app.css (downloads Tailwind CLI to ./bin)
scripts/build-css.sh --watch              # rebuild CSS on template changes
MEDIAHUB_DEMO_MODE=true MEDIAHUB_CONFIG_DIR=./.dev-config MEDIAHUB_LOG_FORMAT=console \
  MEDIAHUB_PORT=8099 uv run python -m mediahub   # run locally with generated data
uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest -q   # all checks
MEDIAHUB_CONFIG_DIR=./.dev-config uv run alembic revision --autogenerate -m "..."  # new migration
```

Re-run `scripts/build-css.sh` after adding Tailwind classes to templates; `static/dist/` is
git-ignored. Classes built from variables (e.g. `text-{{ tone }}`) must be safelisted with
`@source inline(...)` in `static/css/input.css`.

## Commits and releases

- **Conventional Commits, always:** every commit and PR title is `type(scope): subject` (scope
  optional). PRs are squash-merged, so the PR title lands on `main`; a `PR title` check enforces it.
  `feat` = minor; `fix`, `perf`, `deps`, `revert` = patch; `!` or `BREAKING CHANGE:` = breaking
  (minor until 1.0). `docs`, `refactor`, `test`, `build`, `ci`, `chore`, `style` don't release and
  stay out of the changelog, so pick the type by what users get.
- **release-please** owns versions: merging its release PR tags `vX.Y.Z`, creates the GitHub release
  and builds the image. Never bump versions or edit `CHANGELOG.md` by hand. `release-please-config.json`
  keeps `pyproject.toml`, `uv.lock` and `src/mediahub/__init__.py` in step; `uv.lock` must move too,
  or `uv sync --locked` fails in CI and Docker.

## Architecture (src/mediahub/)

- `integrations/<service>/` — one class per service implementing `integrations/base.py:Integration`
  (`test_connection`, `poll(full=)`). Raw API → normalized Pydantic models in `models/`. Never
  touch the DB or templates. Translate HTTP failures via `integrations/http.py` (`get_json`,
  `request`) so errors become `IntegrationError` with user-safe messages.
- `monitoring/` — `Poller` runs one task per target (no overlapping polls, bounded concurrency,
  backoff); `DemandTracker` makes polling fast only while someone is viewing; `SnapshotStore`
  is the in-memory source of truth; `issues.py` derives "Needs Attention" from snapshots;
  `service.py` syncs targets with DB instances (`reload_instance` after any CRUD).
- `monitoring/play_history.py` — the one exception to "monitoring isn't persisted": a poll target
  records each play matched to InfiniDysk into `play_history` (`history/repository.py`, 30 days).
  Local only, never sent to peers, and it keeps no client address or read path.
- `aggregation/dashboard.py` — builds view models for scope `all` / `local` / a peer id.
  Templates only see these view models and normalized models.
- `web/routes/` — page and `/partials/...` handlers read only the store (async, no upstream
  calls). Servers and Settings handlers do DB work via `run_in_threadpool`.
- `db/` — models, `EncryptedJSON` (Fernet) for secrets, `UTCDateTime`; migrations in
  `migrations/versions/` (keep migrations free of app-type imports: use `sa.Text`/`sa.DateTime`).
- `peers/redact.py` — strips IPs/paths before data leaves the server (Phase 5 federation).
- `web/routes/pwa.py` — unauthenticated manifest, `/sw.js` (from `static/js/sw.js`, version
  and precache list filled in) and `/offline`. The service worker must never cache pages or
  partials (live, signed-in data); icons come from `scripts/build-icons.sh`.

## Rules and gotchas

- **Single process only.** Poller + cache are in-process; `runtime_lock.py` enforces one
  process per config dir. Never raise uvicorn `workers`.
- **Secrets:** never render, log, or put API keys in URLs (headers only). Secret form fields are
  write-only (blank = keep stored). The logging redaction processor masks sensitive keys.
- **CSP is strict:** no inline `<script>`, no `hx-on`, no htmx trigger filters (`[cond]`, they
  need eval). Client behaviour goes in `static/js/app.js`.
- **CSRF:** every non-GET route needs it. Router-level `Depends(require_auth), Depends(verify_csrf)`
  — auth must load before CSRF so the session token is used.
- **htmx doesn't swap 4xx responses:** htmx partial forms return 200 with inline errors.
- **Polled sections** use `ui.poll_section(...)` (rendered server-side first, `data-poll`
  gating in app.js). Don't add explicit service routes as `/{param}` catch-alls — they shadow
  `/servers`, `/settings`.
- **InfiniDysk:** only call paths in the read-only allowlist (`assert_read_only`); several of its
  GET routes mutate state.
- Report upstream error/status text verbatim; never invent explanations for failures.
- Python style: `from __future__ import annotations`, ruff line length 100, typed signatures,
  comments only where intent isn't obvious.
- `.dev-config/` is the local runtime dir (DB, secret.key); safe to delete to start fresh.
