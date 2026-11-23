#!/usr/bin/env bash
set -Eeuo pipefail

cd -- "$(dirname -- "$0")"
readonly site_id='d77f6116-74b8-435f-959d-03577bc1a5cb'
readonly base_url='http://127.0.0.1:3000'
compose=(docker compose --env-file .env --env-file images.env)

if [[ ${1:-} == --init && $# == 1 ]]; then
  # This only provisions the reserved synthetic site on a dedicated pilot database.
  "${compose[@]}" exec -T db psql -X -v ON_ERROR_STOP=1 -v site_id="$site_id" -U umami -d umami <<'SQL'
INSERT INTO website (website_id, name, domain, user_id, created_by)
SELECT :'site_id', 'Ops Recovery Pilot Synthetic', 'ops-pilot.invalid', user_id, user_id
FROM "user" WHERE username = 'admin' AND deleted_at IS NULL
ON CONFLICT (website_id) DO NOTHING;
SQL
elif (( $# != 0 )); then
  echo 'Usage: pilot-probe.sh [--init]' >&2
  exit 2
fi

start_ms=$(date +%s%3N)
curl --noproxy '*' --fail --silent --show-error --max-time 8 -o /dev/null "$base_url/api/heartbeat"
event_path="/ops-pilot/synthetic/$(cat /proc/sys/kernel/random/uuid)"
payload=$(printf '{"type":"event","payload":{"website":"%s","hostname":"ops-pilot.invalid","url":"%s","language":"en-US","screen":"1920x1080","name":"pilot.synthetic"}}' "$site_id" "$event_path")
curl --noproxy '*' --fail --silent --show-error --max-time 10 -o /dev/null \
  -H 'Content-Type: application/json' \
  -A 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36' \
  -d "$payload" "$base_url/api/send"

found=$("${compose[@]}" exec -T -e 'PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=5000' \
  db psql -X -v ON_ERROR_STOP=1 -v site_id="$site_id" -v event_path="$event_path" -U umami -d umami -At <<'SQL'
SELECT EXISTS (
  SELECT 1 FROM website_event
  WHERE website_id = :'site_id' AND url_path = :'event_path'
    AND event_name = 'pilot.synthetic' AND created_at >= now() - interval '2 minutes'
);
SQL
)
if [[ $found != t ]]; then
  printf 'SYNTHETIC_EVENT_MISSING path=%s\n' "$event_path" >&2
  exit 1
fi
printf 'SYNTHETIC_EVENT_OK path=%s elapsed_ms=%s\n' "$event_path" "$(($(date +%s%3N) - start_ms))"
