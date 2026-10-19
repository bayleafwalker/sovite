#!/bin/sh
set -eu
: "${SOVITE_STAGE_MODEL:?A model tag is required}"
case "$SOVITE_STAGE_MODEL" in *cloud*|*://*|*..*) echo "Only an approved local model tag can be staged" >&2; exit 2;; esac
ollama serve >/tmp/ollama-staging.log 2>&1 &
stage_pid=$!
trap 'kill "$stage_pid" 2>/dev/null || true' EXIT INT TERM
attempt=0
until ollama list >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -gt 30 ]; then echo "Staging server did not start" >&2; exit 1; fi
  sleep 1
done
ollama pull "$SOVITE_STAGE_MODEL"
ollama show "$SOVITE_STAGE_MODEL" --modelfile
