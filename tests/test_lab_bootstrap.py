"""Tests for the leased-instance bootstrap building blocks (T4.3 + T4.4).

All offline: the sha256-verified install snippet is exercised under `sh` with a
stubbed `curl`/`sha256sum` (no network, no download), and the plans are pure
data. No provisioning, no spend.
"""

import os
import shutil
import subprocess

import pytest

from kannaka_quantum import lab_bootstrap as lb


# ── T4.4: reproducible env + build plan ─────────────────────────────────────


def test_testbed_env_spec_is_fully_pinned():
    spec = lb.testbed_env_spec()
    assert spec["name"] == lb.TESTBED_ENV_NAME
    assert spec["python_version"] == "3.11"
    pkgs = spec["packages"]
    # Every package pins an exact version (reproducible) — no floats/ranges/empties.
    assert pkgs and all(v and v[0].isdigit() for v in pkgs.values())
    assert {"qbraid", "qiskit", "numpy", "pytest", "ruff"} <= set(pkgs)


def test_testbed_build_plan_builds_and_tests_the_engine():
    steps = [s["step"] for s in lb.testbed_build_plan()]
    assert steps.index("create_env") < steps.index("provision") < steps.index("build_engine")
    assert "install_rust" in steps and "test_engine" in steps
    assert steps[-1] == "reap"  # never leave a paid instance running
    # The scenarios variant folds a bench run into the test bed.
    assert any(s["step"] == "bench" for s in lb.testbed_build_plan(scenarios_path="corpus.json"))


# ── T4.3: ephemeral lifecycle plan ──────────────────────────────────────────


def test_ephemeral_lifecycle_plan_order_and_requirements():
    plan = lb.ephemeral_lifecycle_plan("cpu-small", max_minutes=15)
    steps = [s["step"] for s in plan["steps"]]
    assert steps == ["provision", "ssh_configure", "install", "nats_join", "absorb_dream", "drain", "reap"]
    assert plan["max_minutes"] == 15
    # The NATS-creds dependency is surfaced (why the live run may need Nick).
    assert any("NATS" in r for r in plan["requires"])


# ── T4.3: the hard-fail-on-missing-checksum install (security-critical) ──────

_FAKE_CURL = """#!/bin/sh
dest=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) dest="$2"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
case "$url" in
  *.sha256)
    [ "${FAKE_SHA_PRESENT:-1}" = "1" ] || exit 1
    printf '%s  kannaka\\n' "${FAKE_WANT:-aaaa}" > "$dest" ;;
  *) printf 'BINARY-BYTES' > "$dest" ;;
esac
exit 0
"""

_FAKE_SHA256SUM = """#!/bin/sh
printf '%s  %s\\n' "${FAKE_GOT:-aaaa}" "$1"
"""

# Pin uname to a Linux target so the snippet's OS/arch gate passes regardless of
# the host running the test (e.g. Git Bash reports MINGW64_NT).
_FAKE_UNAME = """#!/bin/sh
case "$1" in -s) echo Linux ;; -m) echo x86_64 ;; *) echo Linux ;; esac
"""


def _run_install(tmp_path, env_extra):
    """Run the install snippet under sh with stubbed curl + sha256sum + uname."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("curl", _FAKE_CURL), ("sha256sum", _FAKE_SHA256SUM), ("uname", _FAKE_UNAME)):
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
        **env_extra,
    }
    r = subprocess.run(
        ["sh", "-c", lb.kannaka_install_script()],
        env=env, capture_output=True, text=True, timeout=60,
    )
    installed = (home / ".local" / "bin" / "kannaka").exists()
    return r.returncode, installed, (r.stderr or "")


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh required")
def test_install_succeeds_when_checksum_present_and_matches(tmp_path):
    rc, installed, _ = _run_install(tmp_path, {"FAKE_SHA_PRESENT": "1", "FAKE_WANT": "beef", "FAKE_GOT": "beef"})
    assert rc == 0
    assert installed


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh required")
def test_install_hardfails_when_checksum_missing(tmp_path):
    # THE FIX: a missing .sha256 must be fatal (not a silent unverified install).
    rc, installed, stderr = _run_install(tmp_path, {"FAKE_SHA_PRESENT": "0"})
    assert rc != 0
    assert not installed  # download removed, nothing left on PATH
    assert "checksum" in stderr.lower() and "missing" in stderr.lower()


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh required")
def test_install_fails_on_checksum_mismatch(tmp_path):
    rc, installed, stderr = _run_install(tmp_path, {"FAKE_SHA_PRESENT": "1", "FAKE_WANT": "beef", "FAKE_GOT": "dead"})
    assert rc != 0
    assert not installed
    assert "mismatch" in stderr.lower()
