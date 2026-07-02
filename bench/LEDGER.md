# Hardware fidelity ledger

The longitudinal record of Kannaka's quantum claims measured on real QPUs — the
sim results are the ideal ceiling; these rows are hardware closing (or not
closing) the gap. Quarterly cadence; every row is a guarded, budgeted run.

| # | date | benchmark | device | shots | metric | sim | hardware | cost |
|---|------|-----------|--------|-------|--------|-----|----------|------|
| 0 | 2026-06 | Bell state (2-qubit) | `aws:rigetti:qpu:cepheus-1-108q` | 256 | state fidelity | 100% | **94.5%** (5.5% leakage) | ~$0.41 |
| 1 | 2026-07-01 | recall-correspondence (5-scenario subset) | `aws:rigetti:qpu:cepheus-1-108q` | 200 | agreement (quantum_top == classical_top) | 100% | **40%** (2/5) | **$1.925** (192.5 cr) |

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

## Runbook

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
- Per-minute-billed devices (native `rigetti:rigetti:*` at ~$120/min) are refused outright by the spend guard — always use per-shot devices.
- The CHSH `bell` hardware run (T5.1) is tracked on issue #21 (IQM Garnet offline); when it runs, add its S as another ledger row.
