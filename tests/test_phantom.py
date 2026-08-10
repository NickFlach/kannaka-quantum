"""Network-free tests for the phantom-injection experiment (T5.2, $0/offline).

The falsifiable boundary: injected phantom links — correlations with a shared
local hidden cause — must never produce CHSH-violating statistics. Every
sampled injection and every deterministic local strategy must satisfy |S| ≤ 2;
the sampled bound is exact (not a tolerance) because all four settings share
each hidden-cause draw.
"""

from __future__ import annotations

import json

import pytest

from kannaka_quantum import phantom
from kannaka_quantum.cli import main


def test_cli_phantom_respects_classical_bound(capsys):
    code = main(["phantom", "--shots", "2048"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["bound_respected"] is True
    assert out["max_sampled_abs_S"] <= 2.0
    assert out["deterministic_polytope"]["max_abs_S"] == 2


def test_every_injection_stays_under_the_bound():
    result = phantom.inject(shots=4096)
    for run in result["runs"]:
        assert run["abs_S"] <= 2.0, (
            f"phantom strategy {run['strategy']} @ coupling {run['coupling']} "
            f"produced S={run['S']} > 2 — would falsify phantom=local"
        )
    assert result["bound_respected"] is True


def test_bound_is_sample_exact_not_a_tolerance():
    # Repeated seeds: the same-ensemble construction makes |S| ≤ 2 exact for
    # every sample, not just in expectation — no seed may squeak over.
    for seed in range(7):
        result = phantom.inject(shots=1024, seed=seed)
        assert result["max_sampled_abs_S"] <= 2.0


def test_full_coupling_shared_rounding_touches_the_bound():
    # The sign-threshold LHV model at full coupling sits at the classical
    # maximum for the canonical angles: S ≈ 2 (from below), never above.
    result = phantom.inject(
        strategies=["shared-rounding"], couplings=[1.0], shots=8192
    )
    (run,) = result["runs"]
    assert run["abs_S"] == pytest.approx(2.0, abs=0.08)
    assert run["abs_S"] <= 2.0


def test_perfect_copy_is_maximally_correlated_yet_phantom():
    # Correlation strength is not the tell: E = +1 in every setting, and still
    # S = 1 − 1 + 1 + 1 = 2 exactly. Strong ≠ nonlocal.
    result = phantom.inject(
        strategies=["perfect-copy"], couplings=[1.0], shots=2048
    )
    (run,) = result["runs"]
    for corr in run["correlations"].values():
        assert corr == pytest.approx(1.0)
    assert run["S"] == pytest.approx(2.0)


def test_coupling_sweep_monotone_correlation_never_breaks_bound():
    result = phantom.inject(
        strategies=["perfect-copy"], couplings=[0.0, 0.5, 1.0], shots=4096
    )
    strengths = [run["correlations"]["a0b0"] for run in result["runs"]]
    assert strengths[0] < strengths[1] < strengths[2]  # coupling raises correlation
    for run in result["runs"]:
        assert run["abs_S"] <= 2.0


def test_deterministic_polytope_max_is_exactly_two():
    poly = phantom.enumerate_deterministic_strategies()
    assert len(poly["strategies"]) == 16
    assert poly["max_abs_S"] == 2
    assert any(r["S"] == 2 for r in poly["strategies"])
    assert any(r["S"] == -2 for r in poly["strategies"])


def test_deterministic_and_seeded():
    a = phantom.inject(shots=1024, seed=42)
    b = phantom.inject(shots=1024, seed=42)
    assert a["runs"] == b["runs"]


def test_unknown_strategy_and_bad_coupling_rejected():
    with pytest.raises(ValueError, match="unknown strategy"):
        phantom.inject(strategies=["telepathy"])
    with pytest.raises(ValueError, match="coupling"):
        phantom.inject(couplings=[1.5])
