#!/bin/sh
set -eu
BOOTSTRAP="${REPLAY_BOOTSTRAP_DAYS:-10}"
if [ "${REPLAY_HYDRATE:-1}" != "0" ]; then
  echo "hydrating last ${BOOTSTRAP} replay day(s)…"
  python scripts/download-replay.py --last "${BOOTSTRAP}" || {
    echo "hydrate failed; continuing if local replay/ has days" >&2
  }
fi
exec "$@"
