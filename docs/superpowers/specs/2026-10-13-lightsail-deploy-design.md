# Lightsail deploy for public iron_fly

## Goal

Add `scripts/deploy-lightsail.sh` to the public `iron_fly` repo so a laptop with AWS + Docker can create or update the smallest Lightsail container service and serve the backtest desk over the HTTPS URL Lightsail assigns.

## Decisions

- **Replay strategy:** sync-then-bake. Before `docker build`, pull newest **N** days from the public R2 `manifest.json` into local `replay/` (`N` = `REPLAY_BOOTSTRAP_DAYS`, default 10). Bake those files into the image. Set `REPLAY_HYDRATE=0` on Lightsail so cold starts do not re-download.
- **Manifest source of truth:** day list comes from R2 via existing `scripts/download-replay.py --last N` (which calls `fetch_manifest` / `select_days`).
- **Hybrid fallback:** if the R2 sync fails but local `replay/*.json` already exists, bake those days and continue. If sync fails and `replay/` is empty, **fail the deploy** (do not ship an empty desk).
- **Service shape:** reuse the bot script’s Lightsail layout — service name `iron-fly` (overridable via `LIGHTSAIL_SERVICE`), power `micro`, scale `1`, label `public`, container name `desk`, port `8000` HTTP, health check `/health`.
- **Secrets:** generate or reuse `SESSION_SECRET` in `.lightsail-public.env` (mode 600, gitignored). Never print the secret.

## Deploy flow

1. Require `aws`, `docker`, and `lightsailctl` (PATH or `~/.local/bin`).
2. Resolve AWS region (`AWS_REGION` / `AWS_DEFAULT_REGION` / `aws configure get region`, else `us-east-1`); verify caller identity.
3. Load optional root `.env` for `R2_PUBLIC_BASE` / `REPLAY_BOOTSTRAP_DAYS` (download script already loads `.env`).
4. Run `python3 scripts/download-replay.py --last "$N"`. On failure, continue only if `replay/*.json` is non-empty.
5. Require at least one `replay/*.json`; otherwise exit with a clear error.
6. Ensure / create Lightsail container service; wait until `READY` or `RUNNING`; refuse if power/scale differ from `micro` / `1`.
7. Read service HTTPS URL → `APP_BASE_URL`.
8. `docker build --platform linux/amd64 -t iron-fly:public .`
9. `aws lightsail push-container-image` and parse the returned image name (same parsing as the bot script).
10. `create-container-service-deployment` with containers + public endpoint JSON.
11. Wait until service `RUNNING` and deployment `ACTIVE`; print the public URL.

## Image changes

- Dockerfile: `COPY replay ./replay` after package install so baked days land at `/app/replay` (`REPLAY_CACHE` default).
- Deploy script creates `replay/` before build when needed so `COPY` always has a source directory.
- Entrypoint hydrate remains available for local Docker; Lightsail sets `REPLAY_HYDRATE=0`.

## Container environment

| Variable | Value |
|----------|--------|
| `DESK_MODE` | `backtest-public` |
| `APP_BASE_URL` | Lightsail HTTPS URL (no trailing slash) |
| `SESSION_SECRET` | from `.lightsail-public.env` |
| `DATA_DIR` | `/data` |
| `REPLAY_CACHE` | `/app/replay` |
| `R2_PUBLIC_BASE` | public R2 base (from env / default) |
| `REPLAY_BOOTSTRAP_DAYS` | `N` |
| `REPLAY_HYDRATE` | `0` |

## Repo docs / ignore

- `.gitignore`: add `.lightsail-public.env`.
- README: short “Deploy to Lightsail” section pointing at `./scripts/deploy-lightsail.sh` and prerequisites (aws CLI, Docker, lightsailctl).

## Out of scope

- Custom domain / DNS / CDN in front of Lightsail.
- Changing or removing `iron_fly_bot/scripts/deploy-lightsail.sh`.
- Persistent Lightsail volumes for `/data` (SQLite visitor state resets on redeploy; acceptable for the public desk).
- Baking all manifest days (only newest N).

## Success criteria

- `./scripts/deploy-lightsail.sh` completes with the public HTTPS URL.
- Service state `RUNNING`, deployment `ACTIVE`, health check path `/health` succeeds.
- Desk session picker shows the newest N dates that were present in the R2 manifest at deploy time (or the local fallback set).
