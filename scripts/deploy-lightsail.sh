#!/usr/bin/env bash
# Deploy the public backtest desk to the smallest Lightsail container service.
# Syncs newest N days from the R2 manifest into replay/, bakes them into the image,
# then pushes and deploys. Visitors use the HTTPS address Lightsail assigns.
# TLS ends at that address; the container listens on HTTP port 8000.
# The session secret is kept in .lightsail-public.env and is not printed.
# Usage: ./scripts/deploy-lightsail.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SERVICE="${LIGHTSAIL_SERVICE:-iron-fly}"
POWER="micro"
SCALE="1"
LABEL="public"
LOCAL_IMAGE="iron-fly:public"
SECRET_FILE="$ROOT/.lightsail-public.env"
DEFAULT_R2_BASE="https://pub-2b55d3698db74832b9be7d007e3ef2be.r2.dev"

if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON="$ROOT/.venv/bin/python"
  else
    PYTHON="python3"
  fi
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "the aws CLI is not installed" >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not installed" >&2
  exit 1
fi
if ! command -v lightsailctl >/dev/null 2>&1 && [[ -x "${HOME}/.local/bin/lightsailctl" ]]; then
  export PATH="${HOME}/.local/bin:${PATH}"
fi
if ! command -v lightsailctl >/dev/null 2>&1; then
  echo "lightsailctl is not installed. AWS CLI needs it to push the image." >&2
  echo "https://lightsail.aws.amazon.com/ls/docs/en_us/articles/amazon-lightsail-install-software" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running" >&2
  exit 1
fi

region="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
if [[ -z "$region" ]]; then
  region="$(aws configure get region 2>/dev/null || true)"
fi
if [[ -z "$region" || "$region" == "None" ]]; then
  region="us-east-1"
fi

if ! account="$(aws sts get-caller-identity --query Account --output text --region "$region")"; then
  echo "AWS credentials are not usable. Finish aws login, then run this again." >&2
  exit 1
fi
echo "account ${account} in ${region}"

dotenv_val() {
  local key="$1"
  local line
  [[ -f "$ROOT/.env" ]] || return 0
  line="$(grep -E "^${key}=" "$ROOT/.env" | tail -n 1 || true)"
  [[ -n "$line" ]] || return 0
  printf '%s' "${line#*=}"
}

if [[ -z "${REPLAY_BOOTSTRAP_DAYS:-}" ]]; then
  REPLAY_BOOTSTRAP_DAYS="$(dotenv_val REPLAY_BOOTSTRAP_DAYS || true)"
fi
N="${REPLAY_BOOTSTRAP_DAYS:-10}"

if [[ -z "${R2_PUBLIC_BASE:-}" ]]; then
  R2_PUBLIC_BASE="$(dotenv_val R2_PUBLIC_BASE || true)"
fi
R2_PUBLIC_BASE="${R2_PUBLIC_BASE:-$DEFAULT_R2_BASE}"
R2_PUBLIC_BASE="${R2_PUBLIC_BASE%/}"
export R2_PUBLIC_BASE
export REPLAY_BOOTSTRAP_DAYS="$N"

if ! "$PYTHON" -c 'import httpx' >/dev/null 2>&1; then
  echo "Python at ${PYTHON} cannot import httpx (needed to sync R2)." >&2
  echo "Run: python3.12 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

mkdir -p "$ROOT/replay"
if ! "$PYTHON" "$ROOT/scripts/download-replay.py" --last "$N"; then
  echo "R2 sync failed; checking local replay/" >&2
fi
shopt -s nullglob
replay_days=(replay/*.json)
if [[ ${#replay_days[@]} -eq 0 ]]; then
  echo "replay/ has no days. Fix R2 sync or place YYYY-MM-DD.json files, then retry." >&2
  exit 1
fi
echo "baking ${#replay_days[@]} replay days into the image (newest ${N} from manifest when sync works)"

read_secret() {
  local line
  line="$(grep -E '^SESSION_SECRET=' "$SECRET_FILE" | tail -n 1 || true)"
  printf '%s' "${line#SESSION_SECRET=}"
}

if [[ -f "$SECRET_FILE" ]]; then
  SESSION_SECRET="$(read_secret)"
fi
if [[ -z "${SESSION_SECRET:-}" ]]; then
  SESSION_SECRET="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  umask 077
  printf 'SESSION_SECRET=%s\n' "$SESSION_SECRET" > "$SECRET_FILE"
  chmod 600 "$SECRET_FILE"
  echo "wrote ${SECRET_FILE}"
fi

service_query() {
  aws lightsail get-container-services \
    --region "$region" \
    --service-name "$SERVICE" \
    --query "$1" \
    --output text
}

if ! service_query 'containerServices[0].state' >/dev/null 2>&1; then
  echo "creating ${SERVICE} (${POWER}, scale ${SCALE})"
  aws lightsail create-container-service \
    --region "$region" \
    --service-name "$SERVICE" \
    --power "$POWER" \
    --scale "$SCALE" >/dev/null
fi

echo "waiting for ${SERVICE}"
state=""
for _ in $(seq 1 90); do
  state="$(service_query 'containerServices[0].state')"
  case "$state" in
    READY|RUNNING) break ;;
    FAILED|DISABLED)
      echo "service state is ${state}" >&2
      exit 1
      ;;
  esac
  sleep 10
done
if [[ "$state" != "READY" && "$state" != "RUNNING" ]]; then
  echo "service is still ${state}" >&2
  exit 1
fi

power="$(service_query 'containerServices[0].power')"
scale="$(service_query 'containerServices[0].scale')"
if [[ "$power" != "$POWER" || "$scale" != "$SCALE" ]]; then
  echo "service ${SERVICE} is ${power} scale ${scale}. This script deploys ${POWER} scale ${SCALE}." >&2
  exit 1
fi

url="$(service_query 'containerServices[0].url')"
url="${url%/}"
if [[ "$url" != https://* ]]; then
  echo "Lightsail did not return an https URL (got ${url:-nothing})" >&2
  exit 1
fi

echo "building ${LOCAL_IMAGE} for linux/amd64"
docker build --platform linux/amd64 -t "$LOCAL_IMAGE" "$ROOT"

push_log="$(mktemp)"
aws lightsail push-container-image \
  --region "$region" \
  --service-name "$SERVICE" \
  --label "$LABEL" \
  --image "$LOCAL_IMAGE" >"$push_log"
image="$("$PYTHON" - "$push_log" "$SERVICE" "$LABEL" <<'PY'
import json
import re
import sys

path, service, label = sys.argv[1:]
text = open(path).read()
named = re.findall(r'Refer to this image as "(:[^"]+)"', text)
if named:
    print(named[-1])
    raise SystemExit
blob = re.search(r"\{.*\}", text, re.S)
if blob:
    try:
        image = json.loads(blob.group(0)).get("image") or ""
    except json.JSONDecodeError:
        image = ""
    if image:
        print(image)
        raise SystemExit
found = re.findall(rf"(:{re.escape(service)}\.{re.escape(label)}\.\d+)", text)
if found:
    print(found[-1])
PY
)"
rm -f "$push_log"
if [[ -z "$image" ]]; then
  echo "Lightsail did not return an image name" >&2
  exit 1
fi
echo "pushed ${image}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
"$PYTHON" - "$image" "$url" "$SESSION_SECRET" "$R2_PUBLIC_BASE" "$N" "$tmp" <<'PY'
import json
import sys

image, url, secret, r2_base, bootstrap_days, directory = sys.argv[1:]
containers = {
    "desk": {
        "image": image,
        "environment": {
            "DESK_MODE": "backtest-public",
            "APP_BASE_URL": url,
            "SESSION_SECRET": secret,
            "DATA_DIR": "/data",
            "REPLAY_CACHE": "/app/replay",
            "R2_PUBLIC_BASE": r2_base,
            "REPLAY_BOOTSTRAP_DAYS": bootstrap_days,
            "REPLAY_HYDRATE": "0",
        },
        "ports": {"8000": "HTTP"},
    }
}
endpoint = {
    "containerName": "desk",
    "containerPort": 8000,
    "healthCheck": {
        "path": "/health",
        "successCodes": "200-499",
        "intervalSeconds": 10,
        "timeoutSeconds": 5,
        "healthyThreshold": 2,
        "unhealthyThreshold": 2,
    },
}
with open(f"{directory}/containers.json", "w") as handle:
    json.dump(containers, handle)
with open(f"{directory}/endpoint.json", "w") as handle:
    json.dump(endpoint, handle)
PY
chmod 600 "$tmp/containers.json"

aws lightsail create-container-service-deployment \
  --region "$region" \
  --service-name "$SERVICE" \
  --containers "file://${tmp}/containers.json" \
  --public-endpoint "file://${tmp}/endpoint.json" >/dev/null

echo "waiting for the deployment"
deploy_state=""
for _ in $(seq 1 90); do
  state="$(service_query 'containerServices[0].state')"
  deploy_state="$(service_query 'containerServices[0].currentDeployment.state')"
  if [[ "$state" == "RUNNING" && "$deploy_state" == "ACTIVE" ]]; then
    break
  fi
  if [[ "$deploy_state" == "FAILED" || "$state" == "DISABLED" ]]; then
    echo "deployment failed (${state} ${deploy_state})" >&2
    exit 1
  fi
  sleep 10
done
if [[ "$state" != "RUNNING" || "$deploy_state" != "ACTIVE" ]]; then
  echo "deployment is still ${state} ${deploy_state}" >&2
  exit 1
fi

echo "public desk at ${url}"
echo "HTTPS is on that address. Lightsail forwards to HTTP port 8000 inside the service."
echo "Baked ${#replay_days[@]} replay day(s); REPLAY_HYDRATE=0. Redeploy to refresh tapes from the manifest."
