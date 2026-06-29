# kannaka-quantum

**Real quantum capabilities for [Kannaka](https://github.com/NickFlach/kannaka-memory), executed on actual quantum backends.**

Kannaka's memory is a *Holographic Resonance Medium* — recall is wave interference, and *"attention acts as gravity: wavefronts whose phase/amplitude align with the query are pulled forward."* That is, almost verbatim, the definition of **quantum amplitude amplification**. This package makes the correspondence literal: it runs Kannaka's recall — plus arbitrary circuits and a true-entropy source — on real quantum hardware.

It is a **multi-provider bridge** with **two surfaces over one core**:

- a **JSON CLI** — the Kannaka coding agent shells out to it to write & run quantum programs;
- an **MCP server** — any MCP client (Claude Code, the kannaka-tui harness, other agents) gets the same tools.

---

## Capabilities

| tool (MCP) / subcommand (CLI) | what it does |
|---|---|
| `quantum_devices` / `devices` | List QPUs + simulators across providers, with status, qubit counts, and cost. |
| `run_circuit` / `run` | Execute an **OpenQASM 3** circuit on a backend; returns measurement counts. |
| `quantum_random` / `qrng` | True quantum random bits from measurement collapse (not a PRNG) — a quantum entropy source for the medium's irrationality (Ξ) and dream noise. |
| `resonance_recall` / `recall` | **The showcase.** Amplitude-encode candidate memory resonances into a quantum state and amplitude-amplify toward the strongest — Kannaka's recall, run as interference on a quantum computer. |

---

## Providers & routing

A single **device string** selects both the provider and the backend. The prefix routes:

| device string | provider | notes |
|---|---|---|
| `qbraid:…` | **qBraid** | Default. The free simulator `qbraid:qbraid:sim:qir-sv` (≤28 qubits) needs **no credits**. Real QPUs spend qBraid credits ($0.01 each). |
| `openquantum:…` | **OpenQuantum** (Quantum Rings) | Real QPUs only (IonQ / Rigetti / IQM / AQT) — **no free simulator**; every job spends "Spark" credits (1 credit = $2; free tier 25 credits / $50 per 90 days). Form: `openquantum:<backend>` e.g. `openquantum:iqm:garnet`. |

The free qBraid simulator is the **default device everywhere**, so casual and agent-driven use never spends money. Real hardware runs only when you name a hardware device *and* opt into spending (see [Spend safety](#spend-safety)).

### How each provider is integrated

- **qBraid** — via `qbraid.runtime.QbraidProvider`. `provider.get_device(id).run(qasm3, shots=…)`, then `job.result()`. Live per-task/per-shot/per-minute pricing is read from `device.metadata()['pricing']`.
- **OpenQuantum** — via the `openquantum-sdk` package over **OAuth2 client-credentials**:

  ```python
  from openquantum_sdk import ManagementClient, SchedulerClient
  from openquantum_sdk.auth import ClientCredentials
  from openquantum_sdk.clients import ClientCredentialsAuth, JobSubmissionConfig

  auth  = ClientCredentialsAuth(ClientCredentials(client_id, client_secret))
  mgmt  = ManagementClient(auth=auth)
  sched = SchedulerClient(auth=auth, management_client=mgmt)

  cfg = JobSubmissionConfig(
      backend_class_id="iqm:garnet",      # the part after "openquantum:"
      name="kannaka-quantum",
      job_subcategory_id="phys:oth",       # required workload tag
      shots=256,
      organization_id=mgmt.list_user_organizations(limit=1)[...].id,
      auto_approve_quote=True,
  )
  job    = sched.submit_job(cfg, file_content=qasm.encode("utf-8"))
  output = sched.download_job_output(job)
  ```

  Backends are discovered with `mgmt.list_backend_classes(...)`. The bridge wraps all of this — you only ever pass a device string and OpenQASM.

---

## Install

```bash
pip install kannaka-quantum        # or: pip install -e .   (from this directory)
```

Requires Python ≥ 3.10. Dependencies: `qbraid`, `qiskit`, `numpy`, `mcp`, and `openquantum-sdk`.

---

## Authentication

Configure whichever provider(s) you'll use. The free qBraid simulator works with a qBraid key alone; OpenQuantum is optional and only needed for its real QPUs.

**qBraid** — an API key, resolved in order:
1. `QBRAID_API_KEY`
2. a saved `~/.qbraid/qbraidrc` (`QbraidProvider(api_key=…).save_config()`)
3. `~/Downloads/QBraid.txt` (a workstation convenience; first `qbr_…` match)

**OpenQuantum** — client credentials, resolved in order:
1. `OPENQUANTUM_CLIENT_ID` + `OPENQUANTUM_CLIENT_SECRET`
2. a JSON SDK key at `OPENQUANTUM_SDK_KEY`
3. `~/.openquantum/sdk-key.json`
4. `~/Downloads/sdk-key-*.json` (workstation convenience)

If no OpenQuantum credentials are present, the bridge simply omits OpenQuantum from device listings and stays fully usable on qBraid.

---

## CLI

Every subcommand prints **one JSON object** to stdout (errors included), so a caller can parse it directly.

```bash
kannaka-quantum devices --online
kannaka-quantum run --qasm-file bell.qasm --shots 200
kannaka-quantum qrng --bits 16
kannaka-quantum recall --amplitudes 0.1,0.9,0.2,0.15 --labels alpha,beta,gamma,delta
```

`run` reads OpenQASM 3 from `--qasm`, `--qasm-file`, or stdin (`-`). Spend options (`--allow-spend`, `--max-credits`, `--subcategory`) apply to `run`/`qrng`/`recall`.

### Example: resonance recall

```text
$ kannaka-quantum recall --amplitudes 0.1,0.9,0.2,0.15 --labels alpha,beta,gamma,delta
{"distribution": {"alpha": 2, "beta": 775, "gamma": 240, "delta": 7},
 "quantum_top": "beta", "classical_top": "beta", "agree": true,
 "qubits": 2, "candidates": 4, "amplified": true,
 "device": "qbraid:qbraid:sim:qir-sv"}
```

Amplitude amplification sharpens the prepared resonance state toward the strongest memory — the recall ran on a quantum computer, and it agrees with the classical argmax. The iteration count is derived from the target's *initial* amplitude (`(π/2 − θ)/2θ`), not the textbook `(π/4)√N`, so an already-dominant memory isn't *over*-rotated and de-amplified.

---

## MCP server

```bash
kannaka-quantum mcp        # stdio transport
```

Register with Claude Code:

```bash
claude mcp add kannaka-quantum -- python -m kannaka_quantum mcp
```

…then any agent can call `quantum_devices`, `run_circuit`, `quantum_random`, and `resonance_recall`. (Shipped as a Claude Code plugin too — see `.claude-plugin/` and `skills/kannaka-quantum/`.)

---

## Spend safety

The whole point is that *casual use is free and a careless run can't drain the budget.*

- **Free by default.** The default device is the free qBraid simulator; nothing spends until you name a hardware device.
- **Explicit opt-in.** A real-QPU run requires `allow_spend=True` (CLI `--allow-spend`) or `KANNAKA_QUANTUM_ALLOW_SPEND=1`. Otherwise it raises and points you back to the free simulator.
- **Credit ceiling.** Every paid run is bounded by `max_credits` (CLI `--max-credits`); over-cap pre-flight estimates raise instead of submitting. Defaults: qBraid 200 credits (≈ $2), OpenQuantum 1 credit (≈ $2). Override via `QBRAID_MAX_CREDITS` / `OPENQUANTUM_MAX_CREDITS`.
- **Per-minute devices are refused.** qBraid's *native* Rigetti bills **per minute** (~12000 credits/min ≈ **$120/min**) — cost can't be bounded from a shot count, so the bridge rejects per-minute devices outright. Use a per-shot device instead.

All three hazards (no-opt-in, over-cap, per-minute) raise before any job is submitted — verified at $0.

### Cheap real QPUs

| device | provider | ~cost (256 shots) |
|---|---|---|
| `openquantum:iqm:garnet` | OpenQuantum | $0.00087/shot ≈ $0.22 |
| `openquantum:rigetti:cepheus-1-108q` | OpenQuantum | $0.000255/shot ≈ $0.07 |
| `aws:rigetti:qpu:cepheus-1-108q` | qBraid | 30 + 0.0425/shot credits ≈ $0.41 |
| ⚠️ `rigetti:rigetti:qpu:cepheus-1-108q` | qBraid (native) | **$120/min — refused** |

---

## Verified benchmark (simulator vs real hardware)

Same Bell state, 256 shots:

| run | device | result | leakage |
|---|---|---|---|
| simulator | `qbraid:qbraid:sim:qir-sv` | `00: 122, 11: 134` | 0% |
| real QPU | `aws:rigetti:qpu:cepheus-1-108q` | `00: 127, 11: 115, 01+10: 14` | 5.5% ($0.41) |

≈ 94.5% fidelity under real device noise.

---

## Development

```bash
pip install -e .
pytest                 # 6 network-free tests (no credentials or backend needed)
```

The core (`kannaka_quantum/core.py`) is provider-agnostic; `cli.py` and `mcp_server.py` are thin surfaces over it.

## License

MIT.
