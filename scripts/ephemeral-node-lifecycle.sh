#!/usr/bin/env bash
# T4.3 — ephemeral swarm hemisphere on a leased Lab instance.
#
# Full lifecycle: provision (LEASED, short --max-minutes) -> install the kannaka
# binary sha256-verified (HARD-FAIL on missing checksum) -> join the NATS mesh as
# an ephemeral node -> absorb/dream for the lease window -> graceful NATS drain ->
# lab-reap stops it at expiry. The node appears + dies in the Observatory.
#
# The orchestration logic + install snippet live in kannaka_quantum/lab_bootstrap
# (unit-tested offline). This script wires them to the live CLI. Keep spend tiny:
# cheapest smallest profile, shortest lease, ONE lifecycle.
#
#   scripts/ephemeral-node-lifecycle.sh --profile <slug> [--max-minutes 15] [--dry-run]
#
# Requires: qBraid credits (provision) AND NATS mesh creds reachable on THIS host
# (~/.kannaka-nats.env or config.toml [swarm]) for the join/drain steps. If NATS
# creds are absent, run with --dry-run or expect the join step to fail — the live
# mesh join is the part that needs Nick.
set -euo pipefail

PROFILE=""; MAX_MINUTES=15; DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --max-minutes) MAX_MINUTES="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[ -n "$PROFILE" ] || { echo "usage: $0 --profile <slug> [--max-minutes N] [--dry-run]" >&2; exit 2; }

KQ="${KANNAKA_QUANTUM:-kannaka-quantum}"

if [ "$DRY_RUN" = "1" ]; then
  python -c "import json; from kannaka_quantum import lab_bootstrap as lb; \
print(json.dumps(lb.ephemeral_lifecycle_plan('$PROFILE', max_minutes=$MAX_MINUTES), indent=2))"
  exit 0
fi

echo "==> provision (leased ${MAX_MINUTES}m)"
prov=$("$KQ" lab-provision-instance --profile "$PROFILE" --allow-spend --max-minutes "$MAX_MINUTES" --wait)
echo "$prov"
iid=$(printf '%s' "$prov" | python -c "import sys,json; print(json.load(sys.stdin).get('instance_id') or '')")
[ -n "$iid" ] || { echo "FATAL: no instance_id from provision" >&2; exit 1; }

echo "==> configure ssh"
alias=$("$KQ" lab-ssh-configure --instance-id "$iid" | python -c "import sys,json; print(json.load(sys.stdin)['ssh_alias'])")

echo "==> install kannaka (sha256-verified, hard-fail on missing checksum)"
python -c "from kannaka_quantum import lab_bootstrap as lb; print(lb.kannaka_install_script())" \
  | ssh -o BatchMode=yes "$alias" 'sh -s'

echo "==> join NATS mesh (ephemeral node) — needs NATS creds on the instance"
ssh -o BatchMode=yes "$alias" '~/.local/bin/kannaka swarm join' || \
  echo "!! swarm join failed (NATS creds/mesh config) — see #19; this step needs Nick" >&2

echo "==> absorb/dream for the lease window, then drain"
ssh -o BatchMode=yes "$alias" '~/.local/bin/kannaka dream --mode deep || true'
ssh -o BatchMode=yes "$alias" '~/.local/bin/kannaka swarm drain || true'

echo "==> reap (stops the instance at/after lease expiry)"
"$KQ" lab-reap

echo "Done. Confirm in the Observatory that the node appeared and died; log the \$ cost on #19."
