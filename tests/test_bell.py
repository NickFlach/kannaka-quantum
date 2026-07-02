"""Network-free tests for the CHSH `bell` subcommand (T5.1, sim only).

The simulator must reproduce Tsirelson's bound (S ≈ 2√2) and violate the
classical bound |S| ≤ 2. Real-hardware runs are deferred (see #21) and asserted
to refuse without a spend opt-in — no spend, no network.
"""

from __future__ import annotations

import math

import pytest

from kannaka_quantum import bell, core
from kannaka_quantum.cli import main


def test_cli_bell_violates_classical(capsys):
    import json

    code = main(["bell", "--device", core.LOCAL_DEVICE, "--shots", "8192"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["violates_classical"] is True
    assert out["abs_S"] > 2.7


def test_chsh_violates_classical_bound_on_sim():
    result = bell.chsh(device=core.LOCAL_DEVICE, shots=8192)
    assert result["violates_classical"] is True
    assert result["abs_S"] > 2.7, f"S={result['S']} did not exceed the assertion floor"
    assert result["classical_bound"] == 2.0


def test_chsh_approaches_tsirelson():
    result = bell.chsh(device=core.LOCAL_DEVICE, shots=8192)
    # Within sampling tolerance of 2√2 ≈ 2.8284 at 8192 shots/setting.
    assert result["abs_S"] == pytest.approx(2.0 * math.sqrt(2.0), abs=0.12)
    assert result["S"] <= result["tsirelson_bound"] + 0.12  # can't exceed Tsirelson


def test_chsh_correlators_near_canonical():
    result = bell.chsh(device=core.LOCAL_DEVICE, shots=8192)
    corr = result["correlations"]
    # Three positive ~+0.707, one negative ~-0.707 for the canonical angles.
    assert corr["a0b0"] > 0.6
    assert corr["a1b0"] > 0.6
    assert corr["a1b1"] > 0.6
    assert corr["a0b1"] < -0.6


def test_correlation_decode_perfectly_correlated():
    # All 00/11 -> E = +1; all 01/10 -> E = -1 (local decode: idx = int(bits, 2)).
    assert bell._correlation({"00": 5, "11": 5}, core.LOCAL_DEVICE) == pytest.approx(1.0)
    assert bell._correlation({"01": 5, "10": 5}, core.LOCAL_DEVICE) == pytest.approx(-1.0)
    assert bell._correlation({"00": 5, "01": 5}, core.LOCAL_DEVICE) == pytest.approx(0.0)


def test_bell_real_device_refused_without_opt_in(monkeypatch):
    monkeypatch.delenv("KANNAKA_QUANTUM_ALLOW_SPEND", raising=False)
    with pytest.raises(RuntimeError, match="allow_spend|spend"):
        bell.chsh(device="aws:rigetti:qpu:cepheus-1-108q", shots=256)


def test_bell_angles_are_canonical():
    assert bell.ALICE_ANGLES == (0.0, math.pi / 4)
    assert bell.BOB_ANGLES == (math.pi / 8, 3 * math.pi / 8)
