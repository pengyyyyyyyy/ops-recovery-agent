#!/usr/bin/env bash
set -Eeuo pipefail

bundle=$(cd -- "${1:?Pass the extracted release directory}" && pwd)
release=${2:?Pass a release ID}
if [[ ! $release =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,60}$ ]]; then
  echo 'Invalid release ID' >&2
  exit 2
fi
pilot=/opt/ops-recovery-pilot
agent=/opt/ops-recovery-agent
destination="$agent/releases/$release"
[[ -s $pilot/.env && -s $pilot/images.env && -s $pilot/pilot-probe.sh ]]

if ! id -u ops-recovery >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/ops-recovery-agent --shell /usr/sbin/nologin ops-recovery
fi
install -d -m 0700 -o ops-recovery -g ops-recovery /var/lib/ops-recovery-agent
if [[ ! -f $destination/.ready ]]; then
  umask 022
  install -d -m 0755 "$destination"
  python3 -m venv "$destination/venv"
  "$destination/venv/bin/python" -m pip --disable-pip-version-check install --no-input "$bundle"
  "$destination/venv/bin/ops" session --help >/dev/null
  touch "$destination/.ready"
fi

cd "$pilot"
umask 077
install -m 0644 "$bundle/deploy/umami/load.py" ./load.py
if [[ ! -e active.env ]]; then printf 'UMAMI_PORT=3000\n' > active.env; fi
if [[ ! -e known-good.env ]]; then printf 'UMAMI_PORT=3000\n' > known-good.env; fi
install -m 0600 "$bundle/deploy/umami/compose.yaml" ./compose.candidate.yaml
docker compose -f compose.candidate.yaml --env-file .env --env-file images.env --env-file active.env config --quiet
mv -f compose.candidate.yaml compose.yaml

ln -sfn "$destination/venv" "$agent/runtime.next"
mv -Tf "$agent/runtime.next" "$agent/runtime"
install -m 0644 "$bundle/deploy/umami/"{ops-pilot-broker,ops-recovery-agent,ops-pilot-observer}.service /etc/systemd/system/
install -D -m 0644 "$bundle/deploy/umami/observer.conf" /etc/systemd/system/ops-pilot-probe.service.d/observer.conf
systemctl daemon-reload
systemctl enable ops-pilot-broker.service ops-recovery-agent.service
systemctl restart ops-pilot-broker.service ops-recovery-agent.service
systemctl is-active ops-pilot-broker.service ops-recovery-agent.service ops-pilot-probe.timer
systemctl start ops-pilot-probe.service
systemctl start ops-pilot-observer.service
printf 'AGENT_RUNTIME_READY release=%s\n' "$release"
