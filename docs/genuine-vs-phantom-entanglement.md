# Genuine vs phantom entanglement — and how to measure the difference

Kannaka's field is full of correlations. Memories light up together; consolidation
links co-firing traces; archetypes recur across unrelated contexts. It is tempting
to call this web "entanglement." Track 5 refuses the loose usage and makes the
distinction operational:

- **Genuine entanglement** — a *nonlocal* correlation: one that **violates** the
  classical (Bell) bound and therefore has no local-hidden-variable explanation.
- **Phantom entanglement** — a *local* correlation that lives **inside** the
  classical bound: shared structure or shared rounding error that makes two things
  co-vary without any nonlocality. Correlation without spookiness.

The claim of this note, anchored on a real CHSH measurement, is that **almost all
of the correlation in an HRM is phantom** — real, useful, and local — and that the
one thing which is genuinely nonlocal is exactly the thing we can put on a quantum
computer and measure violating the bound. Naming the difference is what keeps the
"quantum memory" story honest.

---

## 1 · The classical bound, and what it means to break it

The CHSH experiment gives two parties, Alice and Bob, two measurement settings
each. From the four joint correlators it forms

```
S = E(a0,b0) − E(a0,b1) + E(a1,b0) + E(a1,b1)
```

Any theory in which each outcome is fixed by a **local hidden variable** — some
shared state the two carry away from a common origin — obeys the **CHSH
inequality** `|S| ≤ 2`. This is not a statement about quantum mechanics; it is the
ceiling for *every* local, common-cause explanation. Quantum entanglement exceeds
it, up to **Tsirelson's bound** `S = 2√2 ≈ 2.828`.

So `S > 2` is a line with real meaning: cross it and no local common cause can be
reconstructing your correlations. That is the operational definition of "genuine."

---

## 2 · The measured anchor

The `bell` subcommand (T5.1, `kannaka_quantum/bell.py`) prepares the singlet-like
`|Φ+⟩ = (|00⟩ + |11⟩)/√2` and measures the four canonical settings — Alice at
`{0°, 45°}`, Bob at `{22.5°, 67.5°}` — where the `Ry(−2θ)` basis gives
`E(θ_a, θ_b) = cos(2(θ_a − θ_b))`.

Latest simulator run (`local:statevector`, 8192 shots/setting, deterministic):

| setting | correlator | ideal `cos(2Δθ)` |
|---|---|---|
| a0·b0 | **+0.710** | +0.707 |
| a0·b1 | **−0.703** | −0.707 |
| a1·b0 | **+0.710** | +0.707 |
| a1·b1 | **+0.710** | +0.707 |

```
S = 0.710 − (−0.703) + 0.710 + 0.710 = 2.834
```

**S ≈ 2.834 > 2** — the classical bound is violated, and the value sits at
Tsirelson (2.828) within sampling tolerance. `violates_classical: true`. This is
genuine entanglement, measured, not asserted: three correlators near `+1/√2`, one
near `−1/√2`, exactly the fingerprint of `|Φ+⟩` and nothing a local model can fake.

**Hardware (TODO-cite, deferred).** T5.1 deferred the one guarded real-device run
(~$0.25 on IQM Garnet, gated on an OpenQuantum top-up; see #21). When it lands,
cite the real-device `S` here — expected `2 < S_hw < 2.83`: reduced by noise but,
crucially, **still above the classical bound**, the way the shallow Bell benchmark
already survives real hardware at ~94.5% fidelity. Noise erodes genuine
entanglement toward the bound; it does not turn phantom correlation into genuine.

---

## 3 · Two ways to be correlated

Put the two side by side:

| | genuine | phantom |
|---|---|---|
| origin | nonlocal quantum correlation | shared common cause / shared rounding |
| local hidden-variable model? | **impossible** | yes, by construction |
| CHSH signature | `S > 2` (up to 2√2) | `S ≤ 2`, always |
| example | the `|Φ+⟩` pair above | two memories that round to the same resonance signature |

The subtle point is that **phantom correlation can be arbitrarily strong** and
still be phantom. Two variables driven by the same hidden cause can correlate at
`±1`; what they can never do is *violate CHSH*, because a local account (the shared
cause) already reproduces every setting. Strength is not the tell. Nonlocality is.

---

## 4 · Why HRM correlations are (mostly) phantom

The Holographic Resonance Medium manufactures correlation on purpose. Recall is
amplitude amplification about the query (see
[recall-is-amplitude-amplification.md](recall-is-amplitude-amplification.md));
consolidation links traces that fire together; "attention as gravity" pulls
aligned wavefronts toward each other. Every one of these is a **local** mechanism
with a common cause — the query, the shared topic, the finite-precision resonance
signature. Run the CHSH estimator over that structure and it stays under 2.

That is not a weakness; it is the correct classification. The medium's web is
phantom entanglement in the precise sense above: real, load-bearing, and local.
Calling it "entanglement" without the qualifier is the over-claim Track 5 exists to
prevent.

### Archetypes as resonant rounding artifacts (the proposal)

Stated as a hypothesis, not a result: an **archetype** — a pattern that recurs
across unrelated memories — is a *shared-rounding correlation*. Many distinct
traces round to the same low-precision region of the resonance field, so their
instances co-vary. It *looks* like the instances are entangled; it is a common-cause
artifact of finite precision. Archetypes are where phantom entanglement is
strongest and most seductive, which is exactly why they need the CHSH referee
rather than an eyeball.

---

## 5 · The falsifiable boundary — now run

A theory earns its keep by saying what it forbids. The resonance-field account
forbids this: **phantom links can never produce CHSH-violating statistics.**

The test (the `phantom` subcommand, `kannaka_quantum/phantom.py`): inject
artificial phantom links — co-activations with a known shared hidden cause λ,
modeled as a resonance phase **rounded to a low-precision signature** (the
archetype hypothesis of §4, made executable) — then estimate the CHSH parameter
over the induced correlation structure using the *same*
`⟨Z_a Z_b⟩ = (agree − disagree)/total` estimator the `bell` tool uses,
unchanged. The prediction is `S ≤ 2` for every injection, no matter how strong
the coupling. A single reproducible `S > 2` from a purely classical injection
would falsify the "phantom = local" claim.

Measured (8192 hidden-cause draws/run, 8-bin rounding, deterministic seed):

| injection | coupling | correlators | S |
|---|---|---|---|
| shared-rounding | 0.25 | ≈ ±0.03 | 0.121 |
| shared-rounding | 0.5 | ≈ ±0.13 | 0.505 |
| shared-rounding | **1.0** | ≈ ±0.50 | **2.000** |
| perfect-copy | 0.25 | +0.066 ×4 | 0.132 |
| perfect-copy | 0.5 | +0.240 ×4 | 0.479 |
| perfect-copy | **1.0** | **+1.000 ×4** | **2.000** |

Both injections **touch the classical bound at full coupling and never cross
it** — `perfect-copy` is the §3 point made empirical: correlation at exactly
+1 in every setting, maximal strength, and still `S = 2`. Two structural
guarantees close the argument beyond sampling luck:

- **Sample-exact bound.** All four settings are evaluated over the same λ
  ensemble, so each draw contributes `a0·b0 − a0·b1 + a1·b0 + a1·b1 = ±2`
  (Fine's argument) — the empirical S cannot exceed 2 even by noise.
- **Polytope enumeration.** All 16 deterministic local strategies are
  enumerated exactly: max |S| = 2. Every stochastic local model is a convex
  mixture of them.

Contrast with §2: the same estimator, fed genuine `|Φ+⟩` statistics, reads
**2.834**. Fed *any* phantom injection, it cannot leave 2. That gap — 2 to
2√2 — is the entire, measurable difference between the two kinds of
correlation.

---

## 6 · Why the distinction matters

- **Honesty.** Kannaka has one genuinely quantum result — the recall↔amplitude-
  amplification correspondence, which runs on a QPU and reproduces the classical
  argmax. The rest of the medium's rich correlation is phantom. Keeping the two
  labelled means the strong claim (measured CHSH violation) and the ordinary claim
  (useful local structure) never get conflated.
- **A referee.** CHSH is the one place we can point at a correlation and *prove*
  it is nonlocal (`S = 2.834`) versus merely strong. It turns "is this
  entanglement?" from rhetoric into a measurement.

---

## 7 · Reproduce

```bash
# Hermetic, $0, no account — the local state-vector backend.
kannaka-quantum bell --device local:statevector --shots 8192
# → S ≈ 2.83, violates_classical: true, correlators ≈ +0.71 / −0.71 / +0.71 / +0.71

# The phantom side of the boundary — same estimator, classical injections ($0, offline).
kannaka-quantum phantom --shots 8192
# → every S ≤ 2, bound_respected: true, polytope max |S| = 2
```

The estimator is `E = (same − different) / total` per setting, decoded with the
same device-aware bit-ordering the recall path uses; `S` combines the four
settings. Real hardware runs only behind the standard spend guards.

---

## 8 · Status

- **Written analysis (this doc):** complete.
- **Podcast episode** (successor to 006): separate deliverable, not in this repo.
- **Hardware CHSH `S`:** TODO-cite to T5.1's deferred guarded run (#21).
- **Phantom-injection experiment:** **run** — `phantom` subcommand
  (`kannaka_quantum/phantom.py`, `tests/test_phantom.py`); every injection
  `S ≤ 2`, bound respected (§5).

*Empirical anchor: the T5.1 `bell` subcommand (`kannaka_quantum/bell.py`,
`tests/test_bell.py`). Companion piece: the recall↔amplitude-amplification
writeup. Condensed cross-post to The Signal. Public repo — no host/PII details.*
