"""Bootstrap building blocks for leased Lab instances (T4.3 + T4.4).

Two Wave-3 deliverables, kept as small, unit-testable pure functions so the
orchestration can be exercised offline (no provisioning, no spend) and the
runnable scripts under ``scripts/`` stay thin:

- **T4.4** (:func:`testbed_env_spec`, :func:`testbed_build_plan`) — a reproducible
  qBraid env (pinned packages) + an ordered plan to build the ``kannaka-memory``
  Rust engine and run its test suite on a leased instance; the same env is the
  standing quantum test bed for bench / QAOA (T2/T3).
- **T4.3** (:func:`kannaka_install_script`, :func:`ephemeral_lifecycle_plan`) —
  the ordered lifecycle for an ephemeral swarm node: provision (leased) → install
  the sha256-verified binary → join NATS → absorb/dream → graceful drain on reap.

The install snippet embodies the **hard-fail-on-missing-checksum** fix: the
plugin installer (kannaka-plugin/install/install.sh) only verifies the checksum
*if* it can download the ``.sha256`` — a missing checksum silently installs an
unverified binary. Here a missing checksum is fatal.
"""

from __future__ import annotations

from typing import Any, Optional

#: Reproducible qBraid env for the quantum test bed. Pins the kannaka-quantum
#: runtime + test deps so bench/QAOA runs are deterministic across rebuilds.
#: (The Rust engine itself is built via rustup in the bootstrap, not pip.)
TESTBED_ENV_NAME = "kannaka-quantum-testbed"
TESTBED_ENV_PYTHON = "3.11"
TESTBED_ENV_PACKAGES = {
    "qbraid": "0.12.1",
    "qbraid-core": "0.3.4",
    "qiskit": "1.3.1",
    "numpy": "1.26.4",
    "openquantum-sdk": "0.3.7",
    "pytest": "8.3.4",
    "ruff": "0.15.20",
}

#: Default release repo the binary is pulled from (mirrors install.sh).
DEFAULT_RELEASE_REPO = "NickFlach/kannaka-memory"


def testbed_env_spec() -> dict[str, Any]:
    """The lab_create_env spec for the reproducible test bed (pinned packages)."""
    return {
        "name": TESTBED_ENV_NAME,
        "description": "Reproducible kannaka-quantum test bed (bench/QAOA) + kannaka-memory CI",
        "python_version": TESTBED_ENV_PYTHON,
        "packages": dict(TESTBED_ENV_PACKAGES),
        "visibility": "private",
        "tags": ["kannaka", "quantum-wave", "testbed", "ci"],
    }


def kannaka_install_script(release_repo: str = DEFAULT_RELEASE_REPO) -> str:
    """A POSIX-sh snippet that installs the kannaka binary **only if** its
    sha256 checksum is present AND matches — the hard-fail-on-missing-checksum
    fix. A missing ``.sha256`` (or a mismatch) removes the download and exits
    non-zero rather than installing an unverified binary.
    """
    return f'''set -eu
DEST="$HOME/.local/bin"; mkdir -p "$DEST"
os=$(uname -s); arch=$(uname -m)
case "$os" in Linux*) o=linux ;; Darwin*) o=macos ;; *) echo "unsupported OS: $os" >&2; exit 1 ;; esac
case "$arch" in x86_64|amd64) a=x86_64 ;; aarch64|arm64) a=aarch64 ;; *) echo "unsupported arch: $arch" >&2; exit 1 ;; esac
asset="kannaka-${{o}}-${{a}}"
base="https://github.com/{release_repo}/releases/latest/download"
curl -fSL "$base/$asset" -o "$DEST/kannaka" || {{ echo "FATAL: download failed for $asset" >&2; exit 1; }}
# HARD-FAIL on missing checksum: we CANNOT verify without it, so refuse to
# install an unverified binary (the fix — install.sh silently skips instead).
if ! curl -fsSL "$base/$asset.sha256" -o "$DEST/.k.sha"; then
  echo "FATAL: checksum $asset.sha256 missing — refusing to install unverified binary" >&2
  rm -f "$DEST/kannaka" "$DEST/.k.sha"; exit 1
fi
want=$(awk '{{print $1}}' "$DEST/.k.sha"); rm -f "$DEST/.k.sha"
if command -v sha256sum >/dev/null 2>&1; then got=$(sha256sum "$DEST/kannaka" | awk '{{print $1}}');
else got=$(shasum -a 256 "$DEST/kannaka" | awk '{{print $1}}'); fi
if [ "$want" != "$got" ]; then
  echo "FATAL: sha256 mismatch (want $want got $got)" >&2
  rm -f "$DEST/kannaka"; exit 1
fi
chmod +x "$DEST/kannaka"
echo "kannaka installed + sha256-verified -> $DEST/kannaka"
'''


def testbed_build_plan(scenarios_path: Optional[str] = None) -> list[dict[str, str]]:
    """Ordered steps (T4.4) to build + test kannaka-memory on a leased instance
    and leave the quantum test bed ready. Structured so a dry-run/mock can assert
    the flow without provisioning."""
    plan = [
        {"step": "create_env", "action": f"lab_create_env({TESTBED_ENV_NAME!r}, pinned packages)"},
        {"step": "provision", "action": "lab_provision_instance(profile, leased) — records a lease (T4.1)"},
        {"step": "ssh_configure", "action": "lab_ssh_configure(instance_id) -> ssh_alias"},
        {"step": "install_rust", "action": "remote: rustup toolchain (stable) via https://sh.rustup.rs"},
        {"step": "build_engine", "action": "remote: git clone kannaka-memory && cargo build --release"},
        {"step": "test_engine", "action": "remote: cargo test --release (capture wall-time)"},
        {"step": "record_metrics", "action": "compare wall-time/cost vs GitHub-hosted runner; log on #20"},
        {"step": "reap", "action": "lab_reap() stops the instance at lease expiry (T4.1)"},
    ]
    if scenarios_path:
        plan.insert(6, {"step": "bench", "action": f"kannaka-quantum bench --scenarios {scenarios_path}"})
    return plan


def ephemeral_lifecycle_plan(
    profile: str,
    max_minutes: int = 15,
    release_repo: str = DEFAULT_RELEASE_REPO,
    nats_url: Optional[str] = None,
) -> dict[str, Any]:
    """The ordered ephemeral-node lifecycle (T4.3): a structured plan usable by
    the runnable script and asserted by the mocked test. Provision → verified
    install → NATS join → absorb/dream → graceful drain on reap."""
    steps = [
        {"step": "provision", "action": f"lab_provision_instance({profile!r}, max_minutes={max_minutes}) — leased"},
        {"step": "ssh_configure", "action": "lab_ssh_configure(instance_id) -> ssh_alias"},
        {"step": "install", "action": "remote: sha256-verified install (hard-fail on missing checksum)"},
        {"step": "nats_join", "action": "remote: kannaka swarm join (ephemeral node) — requires NATS creds"},
        {"step": "absorb_dream", "action": "remote: kannaka hear / dream for the lease window"},
        {"step": "drain", "action": "remote: kannaka swarm drain (graceful) before stop"},
        {"step": "reap", "action": "lab_reap() stops the instance at lease expiry (T4.1)"},
    ]
    return {
        "profile": profile,
        "max_minutes": max_minutes,
        "release_repo": release_repo,
        "nats_url": nats_url,
        "requires": ["qbraid credits", "NATS mesh creds (~/.kannaka-nats.env or config.toml [swarm])"],
        "steps": steps,
    }
