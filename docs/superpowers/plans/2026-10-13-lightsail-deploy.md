# Lightsail Deploy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `scripts/deploy-lightsail.sh` that syncs newest N R2 manifest days into `replay/`, bakes them into the image, and deploys the public desk to Lightsail.

**Architecture:** Adapt `iron_fly_bot/scripts/deploy-lightsail.sh`: add manifest sync via `download-replay.py`, require at least one baked day, `COPY replay` in Dockerfile, set Lightsail env with `REPLAY_HYDRATE=0`.

**Tech Stack:** bash, AWS Lightsail container services, Docker linux/amd64, existing `scripts/download-replay.py`.

## Global Constraints

- Service defaults: name `iron-fly`, power `micro`, scale `1`, label `public`, container `desk`, port `8000`.
- Newest N days from R2 manifest (`REPLAY_BOOTSTRAP_DAYS`, default 10); fail if zero `replay/*.json` after sync/fallback.
- Never print `SESSION_SECRET`; store in `.lightsail-public.env` (gitignored).
- Do not modify `iron_fly_bot`.

---

### Task 1: Dockerfile bake + gitignore

**Files:**
- Modify: `Dockerfile`
- Modify: `.gitignore`

- [x] **Step 1:** Add `COPY replay ./replay` after `pip install` (before entrypoint copy is fine; after install so deps stay cached). Ensure local `replay/` exists before any build that uses this line (deploy script / document for local).

```dockerfile
COPY replay ./replay
```

- [x] **Step 2:** Add `.lightsail-public.env` to `.gitignore`.

- [x] **Step 3:** Commit.

```bash
git add Dockerfile .gitignore
git commit -m "Bake replay/ into the image for Lightsail deploys."
```

---

### Task 2: deploy-lightsail.sh

**Files:**
- Create: `scripts/deploy-lightsail.sh` (executable)

- [x] **Step 1:** Copy structure from bot script; insert sync block after tool checks:

```bash
# load REPLAY_BOOTSTRAP_DAYS / R2 from .env if present (download-replay also loads .env)
N="${REPLAY_BOOTSTRAP_DAYS:-10}"
if [[ -f "$ROOT/.env" ]]; then
  # shellcheck disable=SC1091
  set -a; source "$ROOT/.env"; set +a
  N="${REPLAY_BOOTSTRAP_DAYS:-$N}"
fi
mkdir -p "$ROOT/replay"
if ! python3 "$ROOT/scripts/download-replay.py" --last "$N"; then
  echo "R2 sync failed; checking local replay/" >&2
fi
shopt -s nullglob
replay_days=(replay/*.json)
if [[ ${#replay_days[@]} -eq 0 ]]; then
  echo "replay/ has no days. Fix R2 sync or place YYYY-MM-DD.json files, then retry." >&2
  exit 1
fi
echo "baking ${#replay_days[@]} replay days into the image"
```

- [x] **Step 2:** Pass env into containers JSON: `R2_PUBLIC_BASE`, `REPLAY_BOOTSTRAP_DAYS`, `REPLAY_HYDRATE=0` plus existing desk vars. Resolve `R2_PUBLIC_BASE` default matching `.env.example`.

- [x] **Step 3:** `chmod +x scripts/deploy-lightsail.sh` and commit.

---

### Task 3: README

**Files:**
- Modify: `README.md`

- [x] **Step 1:** Add a “Deploy to Lightsail” subsection under Docker (or after Docker) with prerequisites and `./scripts/deploy-lightsail.sh`.

- [x] **Step 2:** Commit.

---

### Task 4: Smoke checks

- [x] **Step 1:** `bash -n scripts/deploy-lightsail.sh`
- [x] **Step 2:** Confirm Dockerfile has `COPY replay` and `.gitignore` lists `.lightsail-public.env`.
- [x] **Step 3:** Do not run a live Lightsail deploy unless the user asks.
