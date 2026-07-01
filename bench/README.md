# Recall-correspondence benchmark (Quantum-Wave T2.2)

Measures how often Kannaka's resonance recall, run **as a quantum circuit**
(amplitude amplification), lands on the same memory as the classical argmax —
the *agreement rate*. On a noiseless simulator the two should agree ~always;
that they do is the correspondence claim, and a drop is a regression (a broken
oracle, a flipped endianness, a bad diffuser).

## Files

| File | What |
|------|------|
| `corpus.json` | Scenario corpus in `kannaka-recall-bench/1` (hashed labels, ≤16 candidates each), exported by kannaka-memory (`kannaka export-recall-scenarios`, T2.1). Current corpus: 50 real recall scenarios generated 2026-07-01 from the live 387-memory HRM via `kannaka export-recall-scenarios --n 50 --seed 42` (kannaka-memory PR #481). |
| `baseline.json` | Committed agreement-rate baseline (ideal-simulator ceiling). The gate fails a run that drops **> 2 points** below it. |
| `results/sim/DATE.json` | Weekly simulator snapshots committed by `.github/workflows/bench.yml`. |

## Run it

```bash
# Hermetic, $0, no account — the default backend.
kannaka-quantum bench --scenarios bench/corpus.json --baseline bench/baseline.json

# Hosted qBraid free simulator (needs QBRAID_API_KEY, still $0 credits).
kannaka-quantum bench --scenarios bench/corpus.json \
  --device qbraid:qbraid:sim:qir-sv --baseline bench/baseline.json
```

Exit code is non-zero on a regression, so CI (`bench.yml`, weekly + on PR) gates
PRs on it. The workflow uses the qBraid free simulator when `QBRAID_API_KEY` is
set and the local state-vector backend otherwise (an ideal simulation of the
identical circuits).

## Regenerate the baseline

After an intentional corpus change, refresh the committed baseline:

```bash
kannaka-quantum bench --scenarios bench/corpus.json \
  --baseline bench/baseline.json --update-baseline
```

Real-hardware runs (T2.3) go under `results/hw/` and sit below this ideal ceiling
due to noise — the longitudinal record of hardware closing the gap.
