#!/usr/bin/env bash
# T4.3 — ephemeral swarm hemisphere on a leased Lab instance (hardened after the
# 2026-07-02 live run).
#
# Full lifecycle: provision (LEASED, SHORT --max-minutes so reap-by-expiry fires
# within the run) -> install the kannaka binary sha256-verified (HARD-FAIL on
# missing checksum) -> join the NATS mesh AUTHENTICATED -> brief absorb -> graceful
# `swarm leave` -> wait for lease expiry -> lab-reap (LIVE-VERIFIES T4.1 reap on the
# expired lease) -> lab-terminate-instance (full teardown: frees disk, stops ALL
# billing) -> confirm no orphan. The node appears + dies in the Observatory.
#
#   scripts/ephemeral-node-lifecycle.sh --profile <docker-vm-slug> [--max-minutes 5] [--dry-run]
#
# NOTE (learnings baked in):
#  - Profile MUST be a docker-vm/BMA profile (e.g. cpu-4v-6g). Legacy K8s Lab
#    profiles like 2vCPU_4GB are NOT BMA-provisionable (is_bma_profile=False).
#  - The remote node needs BOTH the NATS creds (~/.kannaka-nats.env:
#    NATS_USER/NATS_PASSWORD) AND the URL (config.toml [swarm] nats_url) — a bare
#    `kannaka swarm join` falls back to anon and can't publish. This uploads both.
#  - On Windows, Git-Bash /usr/bin/ssh can't read qBraid's Windows-path Include;
#    use Windows OpenSSH. And strip CR when piping scripts over ssh.
set -euo pipefail

PROFILE=""; MAX_MINUTES=5; DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --max-minutes) MAX_MINUTES="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

KQ="${KANNAKA_QUANTUM:-kannaka-quantum}"

if [ "$DRY_RUN" = "1" ]; then
  python -c "import json; from kannaka_quantum import lab_bootstrap as lb; \
print(json.dumps(lb.ephemeral_lifecycle_plan('${PROFILE:-<profile>}', max_minutes=$MAX_MINUTES), indent=2))"
  exit 0
fi
[ -n "$PROFILE" ] || { echo "usage: $0 --profile <docker-vm-slug> [--max-minutes N] [--dry-run]" >&2; exit 2; }

# Pick an ssh that can read qBraid's generated config (Windows OpenSSH on Windows).
SSH="ssh"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) [ -x "/c/WINDOWS/System32/OpenSSH/ssh.exe" ] && SSH="/c/WINDOWS/System32/OpenSSH/ssh.exe" ;;
esac
ssh_remote() { "$SSH" -o BatchMode=yes "$1" 'sh -s'; }   # reads the script from stdin

INSTANCE_ID=""
cleanup() {  # teardown is non-negotiable: reap (verify) then terminate (free disk)
  [ -n "$INSTANCE_ID" ] || return 0
  echo "==> TEARDOWN: lab-reap (expired lease) then lab-terminate-instance"
  "$KQ" lab-reap || true
  "$KQ" lab-terminate-instance --instance-id "$INSTANCE_ID" || true
  echo "==> no-orphan check:"
  "$KQ" lab-list-instances
}
trap cleanup EXIT

echo "==> provision (leased ${MAX_MINUTES}m, short so reap-by-expiry fires in-run)"
prov=$("$KQ" lab-provision-instance --profile "$PROFILE" --allow-spend --max-minutes "$MAX_MINUTES" --wait)
INSTANCE_ID=$(printf '%s' "$prov" | python -c "import sys,json; print(json.load(sys.stdin).get('instance_id') or '')")
EXPIRES=$(printf '%s' "$prov" | python -c "import sys,json; print((json.load(sys.stdin).get('lease') or {}).get('expires_at') or '')")
[ -n "$INSTANCE_ID" ] || { echo "FATAL: no instance_id from provision" >&2; exit 1; }
echo "    instance=$INSTANCE_ID lease_expires=$EXPIRES"

echo "==> configure ssh"
alias=$("$KQ" lab-ssh-configure --instance-id "$INSTANCE_ID" | python -c "import sys,json; print(json.load(sys.stdin)['ssh_alias'])")

echo "==> install kannaka (sha256-verified, hard-fail on missing checksum)"
python -c "import sys; from kannaka_quantum import lab_bootstrap as lb; sys.stdout.write(lb.kannaka_install_script())" \
  | tr -d '\r' | ssh_remote "$alias"

echo "==> join NATS mesh AUTHENTICATED (creds + URL uploaded via stdin, not argv)"
# shellcheck disable=SC1090
. "$HOME/.kannaka-nats.env"
NATS_URL=$(grep -E '^\s*nats_url' "$HOME/.kannaka/config.toml" | head -1 | sed -E 's/.*=\s*"?([^"]+)"?.*/\1/')
printf 'export NATS_URL=%q NATS_USER=%q NATS_PASSWORD=%q\nnohup ~/.local/bin/kannaka swarm join >~/swarm.log 2>&1 &\nsleep 6\n~/.local/bin/kannaka swarm status 2>&1 | grep -iE "connected|peers" | head -4\n' \
  "$NATS_URL" "$NATS_USER" "$NATS_PASSWORD" | tr -d '\r' | ssh_remote "$alias"

echo "==> brief absorb, then GRACEFUL leave (verb is 'leave', not 'drain')"
printf '~/.local/bin/kannaka remember "ephemeral cloud hemisphere (T4.3)" --importance 0.7 2>&1 | tail -1\n~/.local/bin/kannaka swarm leave 2>&1 | tail -3\n' \
  | tr -d '\r' | ssh_remote "$alias"

echo "==> wait for the lease to expire so lab-reap tears it down by expiry"
if [ -n "$EXPIRES" ]; then
  exp=$(python -c "import calendar,time; print(calendar.timegm(time.strptime('$EXPIRES','%Y-%m-%dT%H:%M:%SZ')))")
  while [ "$(date -u +%s)" -lt "$((exp + 5))" ]; do sleep 10; done
fi
# The EXIT trap runs cleanup(): lab-reap (live-verify) + lab-terminate-instance + no-orphan check.
echo "==> lifecycle done; teardown via trap. Confirm the node appeared+died in the Observatory; log \$ cost on #19."
