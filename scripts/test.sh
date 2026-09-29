#!/usr/bin/env bash
# Full suite against the Firebase Auth + Firestore emulators (Java + Node needed).
# Usage: scripts/test.sh [pytest args]
set -euo pipefail
cd "$(dirname "$0")/.."
export FIREBASE_PROJECT_ID="${FIREBASE_PROJECT_ID:-demo-elite}"
exec npx --yes firebase-tools@14 emulators:exec --only auth,firestore --project "$FIREBASE_PROJECT_ID" \
  "FIREBASE_AUTH_EMULATOR_HOST=127.0.0.1:9099 FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 python -m pytest -p no:warnings -rs $*"
