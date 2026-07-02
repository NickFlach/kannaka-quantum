"""Network-free tests for the QUBO/QAOA consolidation solver (T3.4, ADR-0038).

All QAOA runs use the local state-vector backend (hermetic, $0). Golden QUBOs
in tests/qubo/*.json are solved and checked against a brute-force optimum, so
optimality is verified without hardcoding expected energies. Real-QPU paths are
asserted to refuse without an explicit spend opt-in — no spend, no network.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from kannaka_quantum import core, qubo
from kannaka_quantum.cli import main

GOLDEN_DIR = Path(__file__).resolve().parent / "qubo"
GOLDEN = sorted(GOLDEN_DIR.glob("*.json"))


def _brute_force(problem: dict):
    n = len(problem["variables"])
    best_bits, best_e = None, float("inf")
    for bits in itertools.product([0, 1], repeat=n):
        e = qubo.energy_of(list(bits), problem)
        if e < best_e:
            best_bits, best_e = bits, e
    return best_bits, best_e


# ── golden-file optimality ──────────────────────────────────────────
def test_golden_dir_is_populated():
    assert GOLDEN, "expected golden QUBO fixtures in tests/qubo/"


@pytest.mark.parametrize("path", GOLDEN, ids=lambda p: p.stem)
def test_golden_qubo_qaoa_finds_optimum(path):
    problem = qubo.load_problem(path.read_text(encoding="utf-8"))
    _bits, opt_energy = _brute_force(problem)
    sol = qubo.solve(problem, device=core.LOCAL_DEVICE, shots=2048)
    # QAOA (kept as a sampler) must reach the true optimum energy on the ideal sim.
    assert sol["energy"] == pytest.approx(opt_energy, abs=1e-6), (
        f"{path.name}: QAOA energy {sol['energy']} != optimum {opt_energy}"
    )
    # The returned assignment must actually achieve that energy.
    assign = [int(b) for b in sol["assignment"]]
    assert qubo.energy_of(assign, problem) == pytest.approx(opt_energy, abs=1e-6)
    assert sol["solver"].startswith("qaoa-p")
    assert sol["exact"] is False
    assert sol["format"] == qubo.SOLUTION_FORMAT


def test_deeper_qaoa_improves_expected_energy():
    # On the ADR example, expected energy should generally improve with depth;
    # at minimum the best-of-p1..3 expected energy is <= the p=1 expected energy.
    problem = qubo.load_problem((GOLDEN_DIR / "adr-example.json").read_text(encoding="utf-8"))
    sol = qubo.solve(problem, device=core.LOCAL_DEVICE, shots=1024)
    by_depth = {d["p"]: d["expected_energy"] for d in sol["qaoa_by_depth"]}
    assert min(by_depth.values()) <= by_depth[1] + 1e-6


# ── penalty-fold honored (structural block ignored) ─────────────────
def test_penalty_fold_keeps_solution_feasible():
    problem = qubo.load_problem((GOLDEN_DIR / "budget-fold.json").read_text(encoding="utf-8"))
    sol = qubo.solve(problem, device=core.LOCAL_DEVICE, shots=2048)
    assign = [int(b) for b in sol["assignment"]]
    # The folded pairwise penalty (not the structural block) must steer to <=1 active.
    assert sum(assign) <= 1
    # Highest-value single var (v0, linear -2.0) is the optimum.
    assert assign == [1, 0, 0, 0]
    assert sol["constraints_satisfied"] == {"budget": True}


# ── parse / validate ────────────────────────────────────────────────
def test_load_problem_rejects_wrong_format():
    with pytest.raises(qubo.QuboError, match="unsupported problem format"):
        qubo.load_problem({"format": "nope/1", "variables": [{"id": 0}]})


def test_load_problem_requires_variables():
    with pytest.raises(qubo.QuboError, match="no 'variables'"):
        qubo.load_problem({"format": qubo.QUBO_FORMAT, "variables": []})


def test_load_problem_requires_contiguous_ids():
    with pytest.raises(qubo.QuboError, match="contiguous"):
        qubo.load_problem(
            {"format": qubo.QUBO_FORMAT, "variables": [{"id": 0}, {"id": 2}]}
        )


def test_load_problem_rejects_too_many_vars():
    variables = [{"id": i} for i in range(qubo.QUBO_VAR_CAP + 1)]
    with pytest.raises(qubo.QuboError, match="exceed"):
        qubo.load_problem({"format": qubo.QUBO_FORMAT, "variables": variables})


def test_diagonal_quadratic_rejected():
    problem = {
        "format": qubo.QUBO_FORMAT,
        "variables": [{"id": 0}, {"id": 1}],
        "linear": {},
        "quadratic": {"0,0": 1.0},
    }
    with pytest.raises(qubo.QuboError, match="diagonal"):
        qubo.solve(qubo.load_problem(problem), device=core.LOCAL_DEVICE)


def test_energy_of_matches_definition():
    problem = qubo.load_problem((GOLDEN_DIR / "adr-example.json").read_text(encoding="utf-8"))
    # f([1,0,1]) = -1.7 + -0.9 + (-0.6)*1*1 = -3.2
    assert qubo.energy_of([1, 0, 1], problem) == pytest.approx(-3.2, abs=1e-9)


# ── spend guard (no network, no spend) ──────────────────────────────
def test_real_qpu_refused_without_opt_in(monkeypatch):
    monkeypatch.delenv("KANNAKA_QUANTUM_ALLOW_SPEND", raising=False)
    problem = qubo.load_problem((GOLDEN_DIR / "anticorrelated.json").read_text(encoding="utf-8"))
    with pytest.raises(RuntimeError, match="spends credits"):
        qubo.solve(problem, device="aws:rigetti:qpu:cepheus-1-108q")


def test_openquantum_refused_without_opt_in(monkeypatch):
    monkeypatch.delenv("KANNAKA_QUANTUM_ALLOW_SPEND", raising=False)
    problem = qubo.load_problem((GOLDEN_DIR / "anticorrelated.json").read_text(encoding="utf-8"))
    with pytest.raises(RuntimeError, match="spends credits"):
        qubo.solve(problem, device="openquantum:iqm:garnet")


def test_hosted_free_sim_not_refused(monkeypatch):
    # A device whose id contains "sim" is free — the preflight guard must pass it
    # (we don't actually submit; just confirm no RuntimeError from the guard).
    monkeypatch.delenv("KANNAKA_QUANTUM_ALLOW_SPEND", raising=False)
    qubo._preflight_spend_guard("qbraid:qbraid:sim:qir-sv", allow_spend=False)
    qubo._preflight_spend_guard(core.LOCAL_DEVICE, allow_spend=False)


# ── CLI ─────────────────────────────────────────────────────────────
def test_cli_qubo_solves_from_file(capsys):
    code = main(
        ["qubo", "--problem-file", str(GOLDEN_DIR / "adr-example.json"),
         "--device", core.LOCAL_DEVICE, "--shots", "1024"]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["assignment"] == [True, False, True]
    assert out["energy"] == pytest.approx(-3.2, abs=1e-6)


def test_cli_qubo_real_device_returns_error_json(capsys, monkeypatch):
    monkeypatch.delenv("KANNAKA_QUANTUM_ALLOW_SPEND", raising=False)
    code = main(
        ["qubo", "--problem-file", str(GOLDEN_DIR / "anticorrelated.json"),
         "--device", "aws:rigetti:qpu:cepheus-1-108q"]
    )
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert "error" in out and "spends credits" in out["error"]
