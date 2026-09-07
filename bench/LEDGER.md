# Hardware fidelity ledger

The longitudinal record of Kannaka's quantum claims measured on real QPUs — the
sim results are the ideal ceiling; these rows are hardware closing (or not
closing) the gap. Quarterly cadence; every row is a guarded, budgeted run.

| # | date | benchmark | device | shots | metric | sim | hardware | cost |
|---|------|-----------|--------|-------|--------|-----|----------|------|
| 0 | 2026-06 | Bell state (2-qubit) | `aws:rigetti:qpu:cepheus-1-108q` | 256 | state fidelity | 100% | **94.5%** (5.5% leakage) | ~$0.41 |
| 1 | 2026-07-01 | recall-correspondence (5-scenario subset) | `aws:rigetti:qpu:cepheus-1-108q` | 200 | agreement (quantum_top == classical_top) | 100% | **40%** (2/5) | **$1.925** (192.5 cr) |
| 2 | 2026-08-10 | CHSH / Bell parameter (T5.1 `bell`) | `aws:rigetti:qpu:cepheus-1-108q` | 512 × 4 settings | `S` (classical bound 2, Tsirelson 2.828) | 2.856 | **2.238 ± 0.073** (3.3σ > 2) | **$2.07** (207.04 cr) |
| 3 | 2026-09-07 | decay (ADR-0002): t1 / ramsey / echo | `rigetti:rigetti:qpu:cepheus-1-108q` (native, Quil-T) | 21 × 300 | half-way delay (P(1) mid-point); abort Δ(0→100 us) = 0.74 | n/a (no simulator executes DELAY) | **t1 27.9 us / ramsey 6.1 us / echo 12.5 us** | **$3.60** (359.7 cr) |

## Row 1 — first quarterly recall-correspondence run

5 scenarios from `bench/corpus.json` (the T2.1 live-HRM export) run through
`quantum_recall` on Rigetti Cepheus (107-qubit superconducting), full result in
[`results/hw/rigetti-cepheus-2026-07-01.json`](results/hw/rigetti-cepheus-2026-07-01.json).

- **Agreement 40% vs 100% ideal-sim ceiling.** Each scenario is a 4-qubit circuit:
  `StatePreparation` of a 16-amplitude state + 1–2 amplitude-amplification
  iterations. That depth is well past what a NISQ device holds coherently, so
  readout and 2-qubit-gate noise de-amplify the target on 3 of 5 scenarios.
- **`argmax_mismatches = 0`** — recall's classical argmax matched the corpus's
  recorded `classical_argmax` on every scenario, so the corpus and the classical
  mapping are intact; the entire gap is device noise, not a harness bug.
- **Contrast with row 0:** the shallow 2-qubit Bell circuit holds 94.5% fidelity,
  while the deep 4-qubit recall circuit degrades to 40% agreement — the expected
  depth/noise story, now measured.
- **Cost $1.925** (192.5 qBraid credits): 5 tasks × (30 per-task + 0.0425/shot ×
  200) credits, at $0.01/credit. Under the authorized $2.00 cap. 256 shots would
  have been $2.04 — over cap — so the subset used 200 shots.

## Row 2 — CHSH on hardware (T5.1, closes #21)

Four 2-qubit CHSH settings on Rigetti Cepheus, 512 shots each, via
`kannaka-quantum bell`. Analysis in
[`docs/genuine-vs-phantom-entanglement.md`](../docs/genuine-vs-phantom-entanglement.md) §2.

- **S = 2.238 ± 0.073 — the classical bound violated by 3.3σ**, at 79% of
  Tsirelson. Correlators +0.645 / −0.387 / +0.543 / +0.664 against an ideal
  ±0.707; every one is pulled toward zero by decoherence, `a0·b1` worst at just
  over half magnitude, yet the combination still clears 2.
- **Consistent with row 0, not row 1.** This is another *shallow* 2-qubit circuit,
  and like row 0's 94.5% Bell fidelity it survives hardware — where row 1's deep
  4-qubit recall circuit collapsed to 40%. The depth/noise story holds across all
  three rows.
- **Not run on IQM Garnet**, despite Garnet being back online at $0.00087/shot.
  OpenQuantum's Spark balance is **0** (`spark_credits=0`), and qBraid reports
  `perTask/perShot = 0.0` for every `openquantum:*` device while
  `_run_openquantum` submits with `auto_approve_quote=True` — so that path's cost
  could not be bounded from existing credits. Cepheus was the cheapest QPU with
  unambiguous live pricing.
- **Cost $2.07** (207.04 cr): 4 tasks × (30 per-task + 0.0425/shot × 512).
  Balance 1350.93 → 1143.89 cr. The per-task fee is 120 cr of that — the reason
  512 shots/setting cost only ~27% more than 250 while buying √2 the precision,
  which is what carried the result from a marginal 2.3σ to 3.3σ.

## Row 3 — controlled delay: decoherence as forgetting (ADR-0002)

Pre-registered on OpenBotCity (artifact `b60d41d1`) before the run; result artifact `8a68f9c0`.
Native device only: Braket drops `DELAY`, OpenQuantum rejects it, the simulator fails it. The
abort check (t1 at 0 vs 200 µs: P(1) 0.92 → 0.07, execution time up by exactly shots × delay)
passed first, for $0.19. Sweep: qubit 0, delays 0/2/5/10/20/50/100 µs, 300 shots per point,
21 jobs, **359.7 credits ≈ $3.60**, no job cost more than 21.4 credits.

| arm | P(1) by delay (µs → P(1), SE ≈ 0.02–0.03) | half-way |
|---|---|---|
| t1 `RX(pi); DELAY t` | 0→0.920, 2→0.920, 5→0.840, 10→0.737, 20→0.620, 50→0.360, 100→0.183 | **27.9 µs** |
| ramsey `RX(pi/2); DELAY t; RX(pi/2)` | 0→0.947, 2→0.847, 5→0.747, 10→0.557, 20→0.470, 50→0.493, 100→0.463 | **6.1 µs** |
| echo `RX(pi/2); DELAY t/2; RX(pi); DELAY t/2; RX(pi/2)` | 0→0.050, 2→0.147, 5→0.163, 10→0.200, 20→0.370, 50→0.440, 100→0.437 | **12.5 µs** |

All three pre-registered predictions held: t1 monotone with its half-way inside 5–60 µs; ramsey
faster than t1; echo slower than ramsey (one mid-interval π pulse doubles the phase lifetime).
No refutation fired. Data: `bench/decay-20260907T055501Z.json` (every job id, count table, credits).
### Qubit-to-qubit variation (same protocol, 200 shots per point on qubits 1 and 2)

| qubit | shots | P(1) at 0 µs | t1 half-way µs | ramsey half-way µs | echo half-way µs | echo / ramsey | cost $ | data |
|---|---|---|---|---|---|---|---|---|
| 0 | 300 | 0.92 | 27.9 | 6.1 | 12.5 | 2.1 | 3.60 | `decay-20260907T055501Z.json` |
| 1 | 200 | 0.93 | 18.6 | 1.3 | 4.4 | 3.4 | 2.63 | `decay-20260907T062942Z.json` |
| 2 | 200 | 0.66 | 1.6 | 1.1 | 1.0 | 0.9 | 2.73 | `decay-20260907T063309Z.json` |

Qubit 2 is a bad qubit today: only 0.66 of shots read |1⟩ with no delay at all, and its
population is gone within a few microseconds, so no rehearsal signal can be read from it. On
the two usable qubits the pre-registered ordering holds (ramsey < echo < t1), with the echo
gain 2.0× on qubit 0 and 3.4× on qubit 1. Neighbouring qubits differ by more than an order of
magnitude, so any future run must gate qubits on a zero-delay readout check first
(suggested: skip a qubit with P(1) at 0 µs below 0.85).

Reproduce (from a box with pyquil, e.g. debain2 `~/pyquil-venv`; the Windows box cannot build `quil`):

```bash
kannaka-quantum decay --device rigetti:rigetti:qpu:cepheus-1-108q --delays-us 0,2,5,10,20,50,100 \
  --arms t1,ramsey,echo --shots 300 --allow-spend --max-credits 400 --max-seconds 2 --max-credits-total 600
```

## Runbook

### Reproduce row 2 (CHSH, ~$2.07)
```bash
kannaka-quantum bell \
  --device aws:rigetti:qpu:cepheus-1-108q --shots 512 \
  --allow-spend --max-credits 60
```
`--max-credits 60` is the **per-setting** ceiling (each of the 4 tasks estimates
51.76 cr), hard-bounding the run at ≤ 240 cr ($2.40).

### Reproduce row 1 (aws:rigetti via qBraid, ~$1.93)
```bash
KANNAKA_QUANTUM_ALLOW_SPEND=1 kannaka-quantum bench \
  --scenarios bench/corpus.json \
  --device aws:rigetti:qpu:cepheus-1-108q \
  --limit 5 --shots 200 \
  --allow-spend --max-credits 40 \
  --out bench/results/hw/rigetti-cepheus-<DATE>.json
```
`--max-credits 40` is the per-task ceiling (each task estimates 38.5 cr); it hard-bounds the 5-task aggregate at ≤ 200 cr ($2.00). Confirm `kannaka-quantum devices --online` shows the device ONLINE and check the balance (`lab-credits`) before running.

### Full 50-scenario run on OpenQuantum (post-top-up, ~$1.63)
OpenQuantum has **no per-task fee** (per-shot only), so the full corpus is cheaper there than a subset on qBraid:
```bash
KANNAKA_QUANTUM_ALLOW_SPEND=1 kannaka-quantum bench \
  --scenarios bench/corpus.json \
  --device openquantum:rigetti:cepheus-1-108q \
  --shots 128 \
  --allow-spend --max-credits 1 \
  --out bench/results/hw/oq-rigetti-<DATE>.json
```
50 scenarios × 128 shots × $0.000255/shot ≈ **$1.63**. (OpenQuantum bills in Spark credits, 1 credit = $2; the free tier is 25 credits / $50 per 90 days.) Run this once the OpenQuantum account is topped up to add a full-corpus hardware row.

### Notes
- Per-minute-billed devices (native `rigetti:rigetti:*`, ~$120/min prorated to execution time) need `--max-seconds` (ADR-0002); they are the only route that executes `DELAY`. A 100-shot job is about $0.07.
- The CHSH `bell` hardware run (T5.1) landed as **row 2** on 2026-08-10.
- OpenQuantum's Spark balance was 0 on 2026-08-10; funded again 2026-09-07. Plain circuits run there (Bell pair 93 % correct parity for $0.03); `DELAY` is rejected ("Delay is not supported").
- `bell` submits one job **per CHSH setting** (4 tasks), and `--max-credits` is
  enforced per task — multiply by 4 to get the real ceiling.
