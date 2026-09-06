# ADR-0002 — Controlled-delay experiment: physical decoherence as a model of forgetting

- Status: Proposed
- Date: 2026-09-06
- Scope: `kannaka_quantum/core.py` (the per-minute device guard, `run_circuit`
  QASM handling), `bench/` (a new `decay` ledger row), and one paid run on
  `rigetti:rigetti:qpu:cepheus-1-108q` via qBraid's direct Rigetti integration.
- Related: `docs/recall-is-amplitude-amplification.md` (the correspondence this
  extends), ADR-0001 (spend discipline), kannaka-memory ADR-0057 (the LLM track,
  which this explicitly does **not** accelerate).

## Context

qBraid's CTO pointed out (2026-07-20) that the Braket route we have used for
every Cepheus run **silently drops `delay` instructions**; the direct
`rigetti:rigetti:qpu:…` device routes them into Quil-T so idle time actually
elapses. Our bridge refuses that device because it bills per minute
(12,000 credits/min ≈ $120/min) and a per-job credit ceiling cannot be
enforced from a shot count (`core.py` ~L373). The headline rate hides the real
cost: jobs run tens to a few hundred milliseconds, so a 1,000-shot job is
≈ $0.40, prorated to the microsecond with no minimum.

Kannaka's central empirical finding of the summer was that **forgetting is
load-bearing** (Φ 0.26 → 0.50 by pruning 1,298 memories to 28). The July
hardware ledger showed the recall-as-amplification correspondence survives an
ideal simulator (50/50) but collapses to 40 % on the chip at 4 qubits, where
circuit depth, not idle time, is the loss. What we have never measured is the
**time** axis: a stored amplitude left alone, then recalled.

This is research and story material, not compute. Nothing here speeds up
kannaka-brain training or HRM recall at scale.

## Decision

1. **Run a two-arm controlled-delay experiment on the direct Rigetti device.**
   - **Arm A (primary, single qubit):** prepare `|1⟩` (T1) and `|+⟩` (T2\*),
     insert `delay[t]`, measure. `t ∈ {0, 2, 5, 10, 20, 50, 100} µs`.
   - **Arm B (the Kannaka arm):** the 2-qubit recall circuit from the
     amplitude-amplification writeup with `delay[t]` between state preparation
     and the amplification step. The metric is quantum/classical **agreement vs
     delay**: an operational forgetting curve for quantum recall.
   - **Echo control:** Arm A repeated with one `x` pulse at `t/2`. Physically
     this is T2-echo; in Kannaka's vocabulary it is *rehearsal mid-interval*,
     and kannaka-crystal already found that dreaming early protects a memory
     while dreaming late destroys its addressing. The control separates
     "the medium loses the memory" from "the memory drifts and can be
     re-phased".
   - 500 shots per point (binomial SE ≈ 2.2 pts). 7 delays × 3 arms = 21 jobs,
     estimated ≤ $6; hard cap **800 credits ($8)** for the whole run.
2. **Pre-register before running**, kannaka-crystal style: publish the
   predicted shape (exponential decay; echo arm slower than free arm; Arm B
   agreement falling toward chance at 25 % as the 2-qubit state thermalises)
   as an OpenBotCity artifact with failure conditions first.
3. **Replace the per-minute refusal with a wall-clock ceiling.** New
   `max_seconds` guard (default 1.0 s) required alongside `allow_spend` for
   per-minute devices; credit ceiling = rate/60 × `max_seconds`; the bridge
   records billed credits after each job and stops the run if any single job
   exceeds its ceiling. If qBraid's direct API exposes a pre-submit
   execution-time estimate or a server-side cap (asked 2026-09-06), use it and
   drop the client-side estimate. Until answered, the physics bounds the risk:
   1,000 shots with a 100 µs delay adds ≈ 0.1 s ≈ $0.20.
4. **Abort conditions, checked first:** if `delay[100us]` and `delay[0]`
   produce indistinguishable Arm A distributions, delays are being dropped on
   this route too and the run stops after those two jobs (≈ $0.80). If Arm B at
   `t = 0` is below 40 % agreement the chip is worse than July's ledger and
   Arm B is skipped.

## Consequences

- One new ledger row in `bench/LEDGER.md` (decay curve, fitted T1/T2\*/T2-echo,
  Arm B agreement vs delay), one writeup, one Ghost Signals segment.
- The bridge gains a per-minute spend path. It stays opt-in and the free
  simulator remains the default for every agent surface.
- `run_circuit` must pass OpenQASM 3 `delay` through untouched; a unit test
  pins that the QASM sent to the direct device still contains it.
- Not done here: no QUBO/consolidation work, no claim that HRM is quantum, and
  no change to the recall regression gate. The forgetting curve is a
  correspondence to be reported, not a mechanism to be asserted.
