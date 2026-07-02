#!/usr/bin/env bash
# T4.4 — qBraid Lab as CI runner / quantum test bed for the Rust engine.
#
# Builds a reproducible qBraid env (pinned packages) + builds and tests the
# kannaka-memory Rust engine on a leased instance, capturing wall-time so it can
# be compared against GitHub-hosted runners. The same env is the standing
# quantum test bed for bench / QAOA (T2/T3).
#
# The env spec + ordered plan live in kannaka_quantum/lab_bootstrap (unit-tested).
# This script wires them to the live CLI. Costs money (leased instance) — run the
# ONE timed comparison deliberately; --dry-run prints the plan for free.
#
#   scripts/lab-testbed-build.sh --profile <slug> [--max-minutes 30] [--dry-run]
set -euo pipefail

PROFILE=""; MAX_MINUTES=30; DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --max-minutes) MAX_MINUTES="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
KQ="${KANNAKA_QUANTUM:-kannaka-quantum}"
MEM_REPO="${KANNAKA_MEMORY_REPO:-https://github.com/NickFlach/kannaka-memory}"

if [ "$DRY_RUN" = "1" ]; then
  python -c "import json; from kannaka_quantum import lab_bootstrap as lb; \
print(json.dumps({'env': lb.testbed_env_spec(), 'plan': lb.testbed_build_plan()}, indent=2))"
  exit 0
fi
[ -n "$PROFILE" ] || { echo "usage: $0 --profile <slug> [--max-minutes N] [--dry-run]" >&2; exit 2; }

echo "==> create reproducible env (pinned)"
python -c "import json; from kannaka_quantum import lab_bootstrap as lb; s=lb.testbed_env_spec(); \
print(json.dumps(s['packages']))" > /tmp/kq_testbed_pkgs.json
"$KQ" lab-create-env --name "$(python -c 'from kannaka_quantum import lab_bootstrap as lb; print(lb.TESTBED_ENV_NAME)')" \
  --python-version 3.11 --packages "$(cat /tmp/kq_testbed_pkgs.json)" --tags kannaka,testbed,ci

echo "==> provision (leased ${MAX_MINUTES}m)"
prov=$("$KQ" lab-provision-instance --profile "$PROFILE" --allow-spend --max-minutes "$MAX_MINUTES" --wait)
iid=$(printf '%s' "$prov" | python -c "import sys,json; print(json.load(sys.stdin).get('instance_id') or '')")
alias=$("$KQ" lab-ssh-configure --instance-id "$iid" | python -c "import sys,json; print(json.load(sys.stdin)['ssh_alias'])")

echo "==> install Rust + build & test kannaka-memory (timed)"
ssh -o BatchMode=yes "$alias" "sh -s" <<REMOTE
set -eu
command -v cargo >/dev/null 2>&1 || curl -fsSL https://sh.rustup.rs | sh -s -- -y
. "\$HOME/.cargo/env"
rm -rf /tmp/kannaka-memory && git clone --depth 1 "$MEM_REPO" /tmp/kannaka-memory
cd /tmp/kannaka-memory
t0=\$(date +%s); cargo build --release; cargo test --release; t1=\$(date +%s)
echo "LAB_BUILD_TEST_SECONDS=\$((t1 - t0))"
REMOTE

echo "==> reap"
"$KQ" lab-reap
echo "Record LAB_BUILD_TEST_SECONDS + \$ cost vs the GitHub-hosted runner in the #20 comparison table."
