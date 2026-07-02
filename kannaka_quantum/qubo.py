"""T3.4 — consolidation-as-QUBO solver (QAOA on the free simulator).

Implements the `kannaka-quantum` side of ADR-0038: read a `kannaka-qubo/1`
problem on stdin, emit a `ConsolidationSolution` JSON on stdout. The solver is
QAOA (p = 1..3) via Qiskit, run on the free simulator by default
(``local:statevector``, hermetic/$0) — hardware only under the existing
spend-guard regime (routed through :func:`core.run_qiskit`).

QUBO convention (ADR-0038): binary x in {0,1}, minimize
``f(x) = Σ linearᵢ·xᵢ + Σ quadraticᵢⱼ·xᵢxⱼ``. Constraints are folded into the
linear/quadratic terms by the emitter ("penalty-folded"); the structural
``constraints`` block may be ignored for solving (we only report satisfaction).

The energy of any bitstring is computed directly from x, so the Ising mapping
below only shapes the QAOA sampling distribution — a wrong sign can weaken the
bias but never returns an invalid assignment (we keep the lowest-energy sample).
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import numpy as np

from . import core

#: Problem / solution wire formats (ADR-0038).
QUBO_FORMAT = "kannaka-qubo/1"
SOLUTION_FORMAT = "kannaka-consolidation-solution/1"
#: QAOA statevector optimization is exponential in variable count; refuse beyond
#: this (dreams that need more should use the classical annealer). 16 vars =
#: 65,536 basis states per objective evaluation.
QUBO_VAR_CAP = 16


class QuboError(Exception):
    """A problem document is malformed or too large."""


# ── parsing ─────────────────────────────────────────────────────────
def load_problem(source: str | dict[str, Any]) -> dict[str, Any]:
    """Parse and validate a ``kannaka-qubo/1`` document (JSON string or dict)."""
    if isinstance(source, str):
        try:
            data = json.loads(source)
        except json.JSONDecodeError as exc:
            raise QuboError(f"problem is not valid JSON: {exc}") from exc
    else:
        data = source

    fmt = data.get("format")
    if fmt != QUBO_FORMAT:
        raise QuboError(f"unsupported problem format {fmt!r}; expected {QUBO_FORMAT!r}")
    variables = data.get("variables")
    if not isinstance(variables, list) or not variables:
        raise QuboError("problem has no 'variables'")
    ids = sorted(int(v["id"]) for v in variables)
    if ids != list(range(len(variables))):
        raise QuboError(f"variable ids must be contiguous 0..{len(variables) - 1}, got {ids}")
    if len(variables) > QUBO_VAR_CAP:
        raise QuboError(
            f"{len(variables)} variables exceed the QAOA-sim cap of {QUBO_VAR_CAP}; "
            "use the classical annealer for larger problems"
        )
    return data


def _linear_vector(problem: dict[str, Any], n: int) -> np.ndarray:
    h = np.zeros(n)
    for k, v in (problem.get("linear") or {}).items():
        h[int(k)] = float(v)
    return h


def _quadratic_map(problem: dict[str, Any], n: int) -> dict[tuple[int, int], float]:
    q: dict[tuple[int, int], float] = {}
    for key, v in (problem.get("quadratic") or {}).items():
        a, b = key.split(",")
        i, j = int(a), int(b)
        if i == j:  # a diagonal quadratic term is just linear on a binary var
            raise QuboError(f"quadratic key {key!r} is diagonal; put it in 'linear'")
        i, j = (i, j) if i < j else (j, i)
        if not (0 <= i < n and 0 <= j < n):
            raise QuboError(f"quadratic key {key!r} out of range for {n} variables")
        q[(i, j)] = q.get((i, j), 0.0) + float(v)
    return q


# ── energy ──────────────────────────────────────────────────────────
def _energy_table(n: int, h: np.ndarray, q: dict[tuple[int, int], float]) -> np.ndarray:
    """Energy of every basis state, indexed by qiskit little-endian idx."""
    energies = np.zeros(2**n)
    for idx in range(2**n):
        x = [(idx >> i) & 1 for i in range(n)]
        e = float(np.dot(h, x))
        for (i, j), c in q.items():
            if x[i] and x[j]:
                e += c
        energies[idx] = e
    return energies


def energy_of(assignment: list[int], problem: dict[str, Any]) -> float:
    """Public helper: energy of an explicit 0/1 assignment under a problem."""
    n = len(problem["variables"])
    h = _linear_vector(problem, n)
    q = _quadratic_map(problem, n)
    e = float(np.dot(h, assignment[:n]))
    for (i, j), c in q.items():
        if assignment[i] and assignment[j]:
            e += c
    return e


# ── QAOA ────────────────────────────────────────────────────────────
def _ising(h: np.ndarray, q: dict[tuple[int, int], float]):
    """QUBO (x in {0,1}) -> Ising (z in {-1,+1}) coefficients for the cost layer."""
    n = len(h)
    a = np.zeros(n)  # single-qubit Z coeffs
    b: dict[tuple[int, int], float] = {}  # ZZ coeffs
    for i in range(n):
        a[i] += -h[i] / 2.0
    for (i, j), c in q.items():
        a[i] += -c / 4.0
        a[j] += -c / 4.0
        b[(i, j)] = b.get((i, j), 0.0) + c / 4.0
    return a, b


def _qaoa_ansatz(n: int, a: np.ndarray, b: dict[tuple[int, int], float], p: int):
    """Parameterized QAOA circuit (no measurement) for depth ``p``."""
    from qiskit import QuantumCircuit
    from qiskit.circuit import Parameter

    gammas = [Parameter(f"g{k}") for k in range(p)]
    betas = [Parameter(f"b{k}") for k in range(p)]
    qc = QuantumCircuit(n)
    qc.h(range(n))
    for k in range(p):
        for i in range(n):
            if a[i]:
                qc.rz(2.0 * gammas[k] * a[i], i)
        for (i, j), coeff in b.items():
            if coeff:
                qc.rzz(2.0 * gammas[k] * coeff, i, j)
        for i in range(n):
            qc.rx(2.0 * betas[k], i)
    return qc, gammas + betas


def _expected_energy(bound_circuit, energies: np.ndarray) -> float:
    from qiskit.quantum_info import Statevector

    probs = np.asarray(Statevector.from_instruction(bound_circuit).probabilities(), dtype=float)
    return float(np.dot(probs, energies))


def _optimize_layer(n, a, b, energies, p, *, restarts, rng):
    """Optimize the 2p QAOA angles for one depth; return (best_params, best_expected)."""
    from scipy.optimize import minimize

    ansatz, params = _qaoa_ansatz(n, a, b, p)

    def objective(values: np.ndarray) -> float:
        bound = ansatz.assign_parameters(dict(zip(params, values)))
        return _expected_energy(bound, energies)

    best_vals: Optional[np.ndarray] = None
    best_e = float("inf")
    for _ in range(restarts):
        x0 = rng.uniform(0.0, np.pi, size=2 * p)
        res = minimize(objective, x0, method="COBYLA", options={"maxiter": 100})
        if res.fun < best_e:
            best_e = float(res.fun)
            best_vals = np.asarray(res.x, dtype=float)
    return best_vals, best_e, ansatz, params


def _preflight_spend_guard(device: str, allow_spend: bool) -> None:
    """Fail fast before the (classical) optimization when a real QPU would spend.

    Defense-in-depth: :func:`core.run_qiskit` enforces the real guard at
    submission, but the optimization pass is wasted work if the final run would
    be refused anyway. Simulators (``local:*`` and any id containing ``sim``)
    are always free.
    """
    if device.startswith(core.LOCAL_PREFIX) or "sim" in device.lower():
        return
    if allow_spend or os.environ.get("KANNAKA_QUANTUM_ALLOW_SPEND") == "1":
        return
    raise RuntimeError(
        f"{device} is a real QPU and spends credits. Re-run with allow_spend=True "
        f"(CLI: --allow-spend) or set KANNAKA_QUANTUM_ALLOW_SPEND=1. Use the free "
        f"simulator ({core.DEFAULT_DEVICE}) or {core.LOCAL_DEVICE} for $0."
    )


def solve(
    problem: dict[str, Any],
    *,
    device: str = core.LOCAL_DEVICE,
    shots: int = 1024,
    max_p: int = 3,
    restarts: int = 2,
    seed: int = 7,
    allow_spend: bool = False,
    max_credits: Optional[float] = None,
    subcategory: Optional[str] = None,
) -> dict[str, Any]:
    """Solve a QUBO with QAOA (p = 1..max_p) and return a ConsolidationSolution.

    Optimization runs on a local state-vector (device-independent, classical);
    the final sampling runs on ``device`` via :func:`core.run_qiskit`, so the
    free-sim default is $0 and hardware stays behind the spend guards.
    """
    _preflight_spend_guard(device, allow_spend)
    n = len(problem["variables"])
    h = _linear_vector(problem, n)
    q = _quadratic_map(problem, n)
    energies = _energy_table(n, h, q)
    a, b = _ising(h, q)
    rng = np.random.default_rng(seed)

    # Optimize each depth on the local state-vector; keep the best.
    best = {"p": 1, "expected": float("inf"), "vals": None, "ansatz": None, "params": None}
    per_depth: list[dict[str, Any]] = []
    for p in range(1, max_p + 1):
        vals, exp_e, ansatz, params = _optimize_layer(
            n, a, b, energies, p, restarts=restarts, rng=rng
        )
        per_depth.append({"p": p, "expected_energy": round(exp_e, 6)})
        if vals is not None and exp_e < best["expected"]:
            best = {"p": p, "expected": exp_e, "vals": vals, "ansatz": ansatz, "params": params}

    # Final sampling on the requested backend with the best depth's angles.
    bound = best["ansatz"].assign_parameters(dict(zip(best["params"], best["vals"])))
    bound.measure_all()
    out = core.run_qiskit(
        bound, device=device, shots=shots, allow_spend=allow_spend,
        max_credits=max_credits, subcategory=subcategory,
    )

    # Map every observed bitstring to an assignment + energy (device-aware decode).
    tally: dict[int, int] = {}
    for bits, count in out["counts"].items():
        idx = core._measured_index(bits, device)
        if 0 <= idx < 2**n:
            tally[idx] = tally.get(idx, 0) + int(count)

    if tally:
        best_idx = min(tally, key=lambda i: energies[i])
    else:  # pragma: no cover - a backend returning no counts
        best_idx = int(np.argmin(energies))
    assignment = [bool((best_idx >> i) & 1) for i in range(n)]
    best_energy = float(energies[best_idx])

    samples = [
        (
            [bool((idx >> i) & 1) for i in range(n)],
            round(float(energies[idx]), 6),
            count,
        )
        for idx, count in sorted(tally.items(), key=lambda kv: kv[1], reverse=True)[:8]
    ]

    solution = {
        "format": SOLUTION_FORMAT,
        "problem_id": problem.get("problem_id"),
        "assignment": assignment,
        "energy": round(best_energy, 6),
        "solver": f"qaoa-p{best['p']}",
        "exact": False,  # QAOA is heuristic; the classical annealer owns exact solves
        "samples": samples,
        "device": device,
        "shots": shots,
        "p": best["p"],
        "qaoa_by_depth": per_depth,
        "variables": n,
    }

    constraints = problem.get("constraints")
    if isinstance(constraints, dict) and constraints:
        solution["constraints_satisfied"] = _check_constraints(assignment, constraints)
    return solution


def _check_constraints(assignment: list[bool], constraints: dict[str, Any]) -> dict[str, bool]:
    """Report (do not enforce) satisfaction of the structural constraint block."""
    report: dict[str, bool] = {}
    for name, spec in constraints.items():
        if not isinstance(spec, dict):
            continue
        vars_ = spec.get("vars")
        max_active = spec.get("max_active")
        if isinstance(vars_, list) and isinstance(max_active, (int, float)):
            active = sum(1 for v in vars_ if 0 <= int(v) < len(assignment) and assignment[int(v)])
            report[name] = active <= int(max_active)
    return report
