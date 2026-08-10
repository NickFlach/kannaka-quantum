"""T5.2 — phantom-injection experiment (`phantom` subcommand).

The falsifiable boundary of the genuine-vs-phantom distinction
(docs/genuine-vs-phantom-entanglement.md §5): **phantom links — correlations
with a shared local hidden cause — can never produce CHSH-violating
statistics.** This module injects exactly such links and runs the same
``⟨Z_a Z_b⟩ = (agree − disagree)/total`` estimator the ``bell`` tool uses over
the induced correlation structure. The prediction is ``S ≤ 2`` for every
injection, at every coupling strength; a single reproducible ``S > 2`` from a
purely classical injection would falsify the "phantom = local" claim.

The injection model speaks HRM: the shared hidden cause λ is a resonance phase
carried by two traces from a common origin, and "rounding" quantizes λ to a
low-precision signature — the archetype-as-resonant-rounding-artifact
hypothesis, made executable. Each side's ±1 outcome is a **local** function of
its own measurement setting and the (rounded) λ; nothing else.

Two structural guarantees frame the sampled runs:

- **Sample-exact bound (Fine's argument).** All four CHSH settings are
  evaluated over the *same* ensemble of λ draws, so each draw contributes
  ``a0·b0 − a0·b1 + a1·b0 + a1·b1 = ±2`` (with deterministic per-λ responses,
  the algebraic identity for ±1 values). The empirical S is a mean of ±2
  values and therefore cannot exceed 2 even by sampling noise.
- **Polytope enumeration.** The 16 deterministic local strategies
  (a0, a1, b0, b1 ∈ {±1}) are the extreme points of the local polytope; their
  exact S values are enumerated and max |S| = 2. Every stochastic local
  strategy is a convex mixture of these, so 2 bounds them all.

Everything here is classical arithmetic — no circuits, no devices, no spend.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable
from typing import Any

from . import core
from .bell import ALICE_ANGLES, BOB_ANGLES, CLASSICAL_BOUND, _correlation

#: Setting names in the CHSH combination S = E00 − E01 + E10 + E11.
SETTINGS = (
    ("a0b0", ALICE_ANGLES[0], BOB_ANGLES[0]),
    ("a0b1", ALICE_ANGLES[0], BOB_ANGLES[1]),
    ("a1b0", ALICE_ANGLES[1], BOB_ANGLES[0]),
    ("a1b1", ALICE_ANGLES[1], BOB_ANGLES[1]),
)

#: Default rounding precision: number of equal bins the resonance phase λ is
#: quantized into. Fewer bins = coarser rounding = stronger phantom coupling.
DEFAULT_BINS = 8

# A response function maps (theta_setting, lambda_rounded) -> ±1, locally.
Response = Callable[[float, float], int]


def _round_phase(lam: float, bins: int) -> float:
    """Quantize a phase λ ∈ [0, 2π) to the center of one of ``bins`` equal
    bins — the shared low-precision resonance signature both traces inherit."""
    if bins <= 0:
        return lam
    width = 2.0 * math.pi / bins
    return (math.floor(lam / width) + 0.5) * width


def _sign(x: float) -> int:
    return 1 if x >= 0.0 else -1


def _resp_shared_rounding(theta: float, lam_r: float) -> int:
    """Threshold the rounded shared phase against the setting's axis, mirroring
    the ``Ry(−2θ)`` convention: outcome = sign(cos(λ_r − 2θ)). The classic
    linear-correlation LHV model; touches S = 2 at the canonical angles."""
    return _sign(math.cos(lam_r - 2.0 * theta))


def _resp_perfect_copy(theta: float, lam_r: float) -> int:
    """Both sides copy the shared signature and ignore their settings —
    correlation +1 in every setting (maximally strong, maximally phantom)."""
    return _sign(math.cos(lam_r))


#: Built-in injection strategies: name -> (alice_response, bob_response).
STRATEGIES: dict[str, tuple[Response, Response]] = {
    "shared-rounding": (_resp_shared_rounding, _resp_shared_rounding),
    "perfect-copy": (_resp_perfect_copy, _resp_perfect_copy),
}


def _inject_counts(
    alice: Response,
    bob: Response,
    *,
    shots: int,
    coupling: float,
    bins: int,
    seed: int,
) -> dict[str, dict[str, int]]:
    """Sample ``shots`` shared hidden causes and tabulate per-setting counts in
    the same ``{bits: count}`` shape a backend returns, so the bell estimator
    consumes them unchanged.

    ``coupling`` ∈ [0, 1]: with probability 1 − coupling a side ignores λ and
    answers with an independent fair coin — the knob that sweeps correlation
    strength without ever leaving the local model. All four settings reuse the
    same λ (and the same decoupling coins), keeping the per-draw CHSH
    contribution ±2.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    lams = rng.uniform(0.0, 2.0 * math.pi, size=shots)
    # Per-draw decoupling decisions and replacement coins, shared across
    # settings so each draw stays a single local hidden state.
    a_keep = rng.random(shots) < coupling
    b_keep = rng.random(shots) < coupling
    a_coin = np.where(rng.random(shots) < 0.5, 1, -1)
    b_coin = np.where(rng.random(shots) < 0.5, 1, -1)

    counts: dict[str, dict[str, int]] = {name: {} for name, _, _ in SETTINGS}
    for i in range(shots):
        lam_r = _round_phase(float(lams[i]), bins)
        for name, theta_a, theta_b in SETTINGS:
            a = alice(theta_a, lam_r) if a_keep[i] else int(a_coin[i])
            b = bob(theta_b, lam_r) if b_keep[i] else int(b_coin[i])
            # +1 -> bit 0, −1 -> bit 1; local decode reads idx&1 as Alice,
            # (idx>>1)&1 as Bob, so the key is f"{bob_bit}{alice_bit}".
            key = f"{0 if b > 0 else 1}{0 if a > 0 else 1}"
            counts[name][key] = counts[name].get(key, 0) + 1
    return counts


def _chsh_from_counts(counts: dict[str, dict[str, int]]) -> tuple[float, dict[str, float]]:
    """The bell tool's own estimator, applied per setting, combined into S.

    S is combined from the *unrounded* correlators (rounding first can smear a
    sample-exact S = 2 into 2.000001 and fake a bound violation); rounding is
    display-only, applied at the end.
    """
    exact = {
        name: _correlation(counts[name], core.LOCAL_DEVICE) for name, _, _ in SETTINGS
    }
    s = exact["a0b0"] - exact["a0b1"] + exact["a1b0"] + exact["a1b1"]
    return round(s, 6), {name: round(v, 6) for name, v in exact.items()}


def enumerate_deterministic_strategies() -> dict[str, Any]:
    """Exact S for all 16 deterministic local strategies — the extreme points
    of the local polytope. Their max |S| is the classical bound itself."""
    results = []
    for a0, a1, b0, b1 in itertools.product((1, -1), repeat=4):
        s = a0 * b0 - a0 * b1 + a1 * b0 + a1 * b1
        results.append({"a0": a0, "a1": a1, "b0": b0, "b1": b1, "S": s})
    max_abs = max(abs(r["S"]) for r in results)
    return {"strategies": results, "max_abs_S": max_abs}


def inject(
    *,
    strategies: list[str] | None = None,
    couplings: list[float] | None = None,
    shots: int = 4096,
    bins: int = DEFAULT_BINS,
    seed: int = 5,
) -> dict[str, Any]:
    """Run the phantom-injection experiment and report every S.

    Returns a JSON-able dict: one row per (strategy, coupling) with the four
    correlators and S, the deterministic-polytope enumeration, and the verdict
    ``bound_respected`` — True iff every sampled and enumerated |S| ≤ 2.
    """
    names = strategies if strategies else list(STRATEGIES)
    unknown = [n for n in names if n not in STRATEGIES]
    if unknown:
        raise ValueError(
            f"unknown strategy {unknown}; available: {sorted(STRATEGIES)}"
        )
    sweep = couplings if couplings is not None else [0.25, 0.5, 1.0]
    for c in sweep:
        if not 0.0 <= c <= 1.0:
            raise ValueError(f"coupling {c} outside [0, 1]")

    runs = []
    for name in names:
        alice, bob = STRATEGIES[name]
        for c in sweep:
            counts = _inject_counts(
                alice, bob, shots=shots, coupling=c, bins=bins, seed=seed
            )
            s, corr = _chsh_from_counts(counts)
            runs.append(
                {
                    "strategy": name,
                    "coupling": c,
                    "S": s,
                    "abs_S": round(abs(s), 6),
                    "correlations": corr,
                }
            )

    polytope = enumerate_deterministic_strategies()
    max_sampled = max(r["abs_S"] for r in runs)
    bound_respected = (
        max_sampled <= CLASSICAL_BOUND and polytope["max_abs_S"] <= CLASSICAL_BOUND
    )
    return {
        "runs": runs,
        "deterministic_polytope": {
            "count": len(polytope["strategies"]),
            "max_abs_S": polytope["max_abs_S"],
        },
        "max_sampled_abs_S": max_sampled,
        "classical_bound": CLASSICAL_BOUND,
        "bound_respected": bound_respected,
        "shots": shots,
        "bins": bins,
        "seed": seed,
        "estimator": "bell._correlation ((agree - disagree)/total), unchanged",
        "note": (
            "all four settings share each hidden-cause draw, so every draw's "
            "CHSH contribution is +/-2 and the empirical S cannot exceed 2 "
            "(Fine's argument) - the locality of the injection enforces the "
            "bound sample-exactly"
        ),
    }
