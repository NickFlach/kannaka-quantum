"""T5.1 — CHSH inequality test (`bell` subcommand).

Prepares a Bell pair and measures the four canonical CHSH settings, computing
the Bell parameter S. A local/simulator run reproduces Tsirelson's bound
S ≈ 2√2 ≈ 2.828, violating the classical bound |S| ≤ 2 — the empirical anchor
for the genuine-vs-phantom entanglement distinction (Track 5).

The correlation for the |Φ+⟩ state measured with an ``Ry(-2θ)`` basis rotation
is E(θ_a, θ_b) = cos(2(θ_a − θ_b)); with Alice angles {0, π/4} and Bob angles
{π/8, 3π/8},

    S = E(a0,b0) − E(a0,b1) + E(a1,b0) + E(a1,b1) = 2√2.

Runs on the free simulator by default (``local:statevector``, hermetic/$0);
real hardware only under the standard spend guards via :func:`core.run_qiskit`.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from . import core

#: Canonical CHSH measurement angles (radians) for maximal violation.
ALICE_ANGLES = (0.0, math.pi / 4)  # 0°, 45°
BOB_ANGLES = (math.pi / 8, 3 * math.pi / 8)  # 22.5°, 67.5°
CLASSICAL_BOUND = 2.0
TSIRELSON_BOUND = 2.0 * math.sqrt(2.0)  # ≈ 2.8284


def _chsh_circuit(theta_a: float, theta_b: float):
    """Bell pair |Φ+⟩ with Alice/Bob measured in Ry(-2θ)-rotated bases."""
    from qiskit import QuantumCircuit

    qc = QuantumCircuit(2, 2)
    qc.h(0)
    qc.cx(0, 1)
    qc.ry(-2.0 * theta_a, 0)
    qc.ry(-2.0 * theta_b, 1)
    qc.measure([0, 1], [0, 1])
    return qc


def _correlation(counts: dict[str, int], device: str) -> float:
    """<Z_a Z_b> from counts: (same − different) / total, device-aware decode."""
    total = 0
    signed = 0
    for bits, count in counts.items():
        idx = core._measured_index(bits, device)
        a = idx & 1
        b = (idx >> 1) & 1
        signed += (1 if a == b else -1) * int(count)
        total += int(count)
    return signed / total if total else 0.0


def chsh(
    *,
    device: str = core.LOCAL_DEVICE,
    shots: int = 4096,
    allow_spend: bool = False,
    max_credits: Optional[float] = None,
    subcategory: Optional[str] = None,
) -> dict[str, Any]:
    """Run the four CHSH settings and compute the Bell parameter S.

    Returns a JSON-able dict with S, the four correlators, the angles, and
    whether the classical bound was violated. On a noiseless simulator S ≈ 2√2.
    """
    settings = [
        ("a0b0", ALICE_ANGLES[0], BOB_ANGLES[0]),
        ("a0b1", ALICE_ANGLES[0], BOB_ANGLES[1]),
        ("a1b0", ALICE_ANGLES[1], BOB_ANGLES[0]),
        ("a1b1", ALICE_ANGLES[1], BOB_ANGLES[1]),
    ]
    correlations: dict[str, float] = {}
    job_ids: dict[str, Any] = {}
    for name, theta_a, theta_b in settings:
        out = core.run_qiskit(
            _chsh_circuit(theta_a, theta_b),
            device=device,
            shots=shots,
            allow_spend=allow_spend,
            max_credits=max_credits,
            subcategory=subcategory,
        )
        correlations[name] = round(_correlation(out["counts"], device), 6)
        job_ids[name] = out.get("job_id")

    s = (
        correlations["a0b0"]
        - correlations["a0b1"]
        + correlations["a1b0"]
        + correlations["a1b1"]
    )
    return {
        "S": round(s, 6),
        "abs_S": round(abs(s), 6),
        "correlations": correlations,
        "classical_bound": CLASSICAL_BOUND,
        "tsirelson_bound": round(TSIRELSON_BOUND, 6),
        "violates_classical": abs(s) > CLASSICAL_BOUND,
        "alice_angles_rad": list(ALICE_ANGLES),
        "bob_angles_rad": list(BOB_ANGLES),
        "device": device,
        "shots": shots,
        "job_ids": job_ids,
    }
