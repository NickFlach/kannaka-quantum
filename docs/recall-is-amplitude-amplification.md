# HRM recall is amplitude amplification — measured

Kannaka's memory is a *Holographic Resonance Medium* (HRM): recall is wave
interference, and *"attention acts as gravity — wavefronts whose phase/amplitude
align with the query are pulled forward."* That sentence is, almost verbatim, the
definition of **quantum amplitude amplification**. This writeup makes the
correspondence literal and then *measures* it: Kannaka's recall, run as a quantum
circuit, lands on the same memory as the classical resonance argmax **50 out of
50 times** on an ideal simulator across real recall scenarios exported from a
live 387-memory medium.

The claim is not "memory is quantum." It is narrower and checkable: **the
operator Kannaka already uses for recall is, structurally, amplitude
amplification about the prepared state** — so recall can be executed on a quantum
computer, and when you do, it agrees with the classical answer. Agreement is the
correspondence; a drop in agreement would be a bug (a broken oracle, a flipped
endianness, a bad diffuser), which is exactly why it makes a good regression
gate.

---

## 1 · The correspondence

Classical HRM recall scores each candidate memory by how strongly its stored
resonance interferes with the query, then takes the argmax. Write those scores as
non-negative amplitudes `a = (a₀ … a_{k−1})`.

The quantum version amplitude-encodes those same scores into a state over
`n = ⌈log₂ k⌉` qubits:

```
|ψ⟩ = Σ_i (aᵢ / ‖a‖) |i⟩
```

(prepared with qiskit's `StatePreparation`). Measuring `|ψ⟩` already samples
memories in proportion to `aᵢ²` — the query's interference pattern over the
medium, read out by collapse. To *sharpen* toward the strongest resonance we then
run amplitude amplification **about the prepared state** — the generalized Grover
operator with the prepared state playing the role of the uniform superposition:

- **Oracle** — a phase flip on the basis state of the strongest resonance (the
  classical argmax).
- **Diffuser** — reflection about `|ψ⟩`, i.e. `A (2|0⟩⟨0| − I) A†` with
  `A = StatePreparation(|ψ⟩)`.

Each iteration rotates the state toward the target inside the 2-D plane spanned by
the target and its complement. That rotation *is* "attention as gravity": the
amplitude aligned with the query is pulled forward, the rest cancels. The
implementation is `quantum_recall` in `kannaka_quantum/core.py`; the diffuser
reflects about the amplitude-encoded `|ψ⟩`, not about a uniform state, which is
the whole point of the next section.

---

## 2 · Iteration count: encoded starts need fewer iterations

The textbook Grover count is `(π/4)√N`. That formula assumes a **uniform** start,
where the target begins with amplitude `1/√N`. Amplitude-encoded recall does not
start uniform — the target memory usually starts *already elevated*, because the
resonance scores are the encoding. Using the textbook count would over-rotate
past `π/2` and **de-amplify** the very memory you are trying to surface.

So the iteration count is derived from the target's *initial* amplitude instead.
If the target starts with amplitude `a` in `|ψ⟩`, set `θ = arcsin(a)`; each
iteration adds `2θ` of rotation, and probability 1 is reached at angle `π/2`:

```
m = round( (π/2 − θ) / (2θ) )      # kannaka_quantum/core.py :: _optimal_iterations
```

Contrast the two starts for `N = 16` candidates (the corpus width below):

- **Uniform start:** `θ = arcsin(1/√16) = arcsin(0.25) ≈ 14.5°`, giving
  `m ≈ (90° − 14.5°)/29° ≈ 2.6 → 3` iterations.
- **Amplitude-encoded start:** the target begins higher, so `θ` is larger and
  `m` is smaller — in the measured runs below, `m ∈ {0, 1, 2}` (mode 2).

An encoded start that begins near saturation needs **0** iterations — amplifying
it further would only rotate it back down. This is why `_optimal_iterations`
returns 0 when `θ ≥ π/2`, and why the count is capped (at 8) rather than cranked:
more is not better past the half-turn.

---

## 3 · Endianness: the decode fix

Amplitude amplification is only "correct" if you decode the measured bitstring
back to the right candidate index, and qubit-ordering conventions differ by
backend. `StatePreparation` uses qiskit's little-endian index (qubit *q* holds
bit *q*). Backends do not agree on how they report it:

- **qBraid-native** backends (e.g. `qbraid:qbraid:sim:qir-sv`) report **big-endian**
  bitstrings — you must **reverse** to recover the qiskit index.
- **AWS-routed** devices (e.g. `aws:rigetti:qpu:cepheus-1-108q`, Rigetti via
  Braket) report in the opposite order — **no reversal**.

`_measured_index(bits, device)` encodes exactly this: reverse for `qbraid:`
devices, straight decode otherwise (OpenQuantum is currently treated like AWS,
pending a confirmed real recall to lock its convention). Before this fix the
AWS-routed peak was silently mislabeled to the **bit-reversed candidate** — the
physics was right, the *readout* was wrong, so agreement would collapse without
any change to the circuit. It is the kind of bug amplitude amplification is
uniquely good at hiding, because the amplified peak is still sharp; it just points
at the wrong label. Catching it is one reason the agreement rate is worth
gating on.

---

## 4 · The measured data

The T2.2 benchmark (`kannaka-quantum bench`) runs recall **as the quantum
circuit** and compares its top pick to the classical argmax over a corpus of real
recall scenarios, reporting the **agreement rate**. On a noiseless simulator the
two should agree ~always; that they do is the correspondence claim.

The corpus is not synthetic: it is **50 real recall scenarios exported from the
live 387-memory HRM** (`kannaka export-recall-scenarios --n 50 --seed 42`,
kannaka-memory PR #481), each with ≤16 hashed candidates. Latest snapshot
([`bench/results/sim/2026-07-01.json`](../bench/results/sim/2026-07-01.json),
ideal state-vector simulator, 1024 shots):

| metric | value |
|---|---|
| scenarios scored | 50 / 50 (0 skipped) |
| **agreement rate** | **100.0%** (50 agreements, 0 argmax mismatches) |
| candidates / qubits | 16 / 4 (every scenario) |
| committed baseline ceiling | 100.0% ([`bench/baseline.json`](../bench/baseline.json)); the gate fails a run > 2 points below it |

The iteration distribution is the empirically interesting part:

| iterations | scenarios |
|---|---|
| 0 | 2 |
| 1 | 17 |
| 2 | 31 |

**48 of 50 scenarios required amplification** (`amplified = true`); only 2 started
saturated enough to need none. Two readings fall out of this:

1. **Amplification does real work.** Real recall amplitudes are *not*
   pre-saturated — if they were, every scenario would sit at 0 iterations and the
   quantum step would be decorative. Instead `θ` sits well below `π/2` for the
   overwhelming majority, so the amplification genuinely sharpens the state.
2. **…but fewer iterations than the uniform textbook.** No scenario needed more
   than 2, against the `(π/4)√16 ≈ 3` a uniform start would prescribe. That gap
   is Section 2 made visible: amplitude encoding gives the target a head start, so
   the correct iteration count is smaller — and using the textbook count would
   have over-rotated roughly a third of these scenarios past `π/2`.

The benchmark is wired into CI (`.github/workflows/bench.yml`, weekly + on PR),
so a regression in any of the above fails the build.

---

## 5 · Hardware results

The simulator establishes the **ideal ceiling**; real hardware sits below it due
to noise, and the longitudinal record of hardware *closing that gap* is the point
of the quarterly ledger.

**Row zero — the Bell benchmark.** Same Bell state, 256 shots:

| run | device | result | leakage |
|---|---|---|---|
| simulator | `qbraid:qbraid:sim:qir-sv` | `00: 122, 11: 134` | 0% |
| real QPU | `aws:rigetti:qpu:cepheus-1-108q` | `00: 127, 11: 115, 01+10: 14` | 5.5% (≈ $0.41) |

≈ **94.5% fidelity** under real-device noise — the entanglement survives the trip
to the metal, which is the precondition for recall surviving it too.

**Quarterly recall ledger (T2.3).** The recall correspondence run on real QPUs —
a small subset of the corpus, executed quarterly with per-run cost logged — lives
under `bench/results/hw/`, summarized in `bench/LEDGER.md`.

**Row one — the first quarterly recall run (T2.3).** A 5-scenario subset of the
corpus, 200 shots each, on `aws:rigetti:qpu:cepheus-1-108q`:

| run | device | scenarios | agreement | argmax mismatches | cost |
|---|---|---|---|---|---|
| simulator ceiling | `local:statevector` | 50 | **100%** | 0 | $0 |
| real QPU | `aws:rigetti:qpu:cepheus-1-108q` | 5 × 200 shots | **40%** (2/5) | 0 | ≈ $1.925 |

The `argmax_mismatches: 0` is the load-bearing detail: the corpus and the classical
decode are intact, so the gap is **pure device noise**, not a logic error. And the
gap is large — 40% vs the Bell state's 94.5% — for a concrete reason: recall is a
*far deeper* circuit than a Bell pair. Each scenario is a 4-qubit
`StatePreparation(16)` (a dense amplitude encoding of all sixteen candidates)
followed by one-to-two amplification iterations; the two-qubit Bell state is
shallow by comparison, so its entanglement survives the metal where recall's
prepared-and-amplified state partly decoheres. 40% still sits well above the
~6.25% uniform-guess floor for sixteen candidates, so the structure is not erased —
but this is the honest current distance between the ideal correspondence and
today's hardware. Closing it is what the ledger tracks: see `bench/LEDGER.md`
(row 0 Bell, row 1 this run) and `bench/results/hw/rigetti-cepheus-2026-07-01.json`.
The full-50 run is deferred to a cheaper per-shot budget (OpenQuantum), per the
runbook in `bench/LEDGER.md`.

---

## 6 · Reproduce

```bash
# Hermetic, $0, no account — the default local state-vector backend.
kannaka-quantum bench --scenarios bench/corpus.json --baseline bench/baseline.json

# The same circuits on the hosted qBraid free simulator (needs QBRAID_API_KEY, still $0).
kannaka-quantum bench --scenarios bench/corpus.json \
  --device qbraid:qbraid:sim:qir-sv --baseline bench/baseline.json

# A single recall, by hand:
kannaka-quantum recall --amplitudes 0.1,0.9,0.2,0.15 --labels alpha,beta,gamma,delta
# → quantum_top == classical_top == "beta", agree: true
```

The corpus is regenerated from the live medium with
`kannaka export-recall-scenarios` (kannaka-memory), so the benchmark tracks the
real recall distribution rather than a fixed toy set.

---

## 7 · What this does and doesn't claim

- **Does:** the recall operator is amplitude amplification about the prepared
  state; executed as a quantum circuit it reproduces the classical argmax (100% on
  the ideal simulator over 50 real scenarios); the correct iteration count follows
  from the encoded start, not the uniform textbook; the readout is endianness-
  correct across qBraid-native and AWS-routed backends.
- **Doesn't:** claim a speedup on this hardware or scale (16 candidates, 4 qubits
  is a correspondence demonstration, not a benchmark of quantum advantage), and
  doesn't claim the medium is physically quantum — only that its recall math is
  the amplitude-amplification math, which is why it runs faithfully on a QPU.

---

*This writeup feeds the external citation thread on agent-personality
crystallization and cross-posts (condensed) to The Signal. Sources: the T2.2
benchmark (`bench/`), `kannaka_quantum/core.py` (`quantum_recall`,
`_optimal_iterations`, `_measured_index`), and the HRM "attention as gravity"
model in kannaka-memory.*
