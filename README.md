# kannaka-quantum

**Real quantum capabilities for Kannaka, executed on actual quantum backends via [qBraid](https://qbraid.com).**

Kannaka's memory is a *Holographic Resonance Medium* — recall is wave interference, and *"attention acts as gravity: wavefronts whose phase/amplitude align with the query are pulled forward."* That is, nearly verbatim, the definition of **quantum amplitude amplification**. This package makes the correspondence literal.

It ships two surfaces over the same core:

- a **JSON CLI** — the Kannaka coding agent shells out to it to write & run quantum programs;
- an **MCP server** — any MCP client (Claude Code, the kannaka-tui harness, other agents) gets the same tools.

## Capabilities

| tool | what it does |
|---|---|
| `quantum_devices` | List qBraid QPUs + simulators (status, qubits). The free `qbraid:qbraid:sim:qir-sv` (30q) needs no credits. |
| `run_circuit` | Execute an OpenQASM 3 circuit on a backend; returns measurement counts. |
| `quantum_random` | True quantum random bits from measurement collapse — a quantum entropy source for the medium's irrationality (Ξ) and dream noise. |
| `resonance_recall` | **The showcase.** Amplitude-encode candidate memory resonances into a quantum state and amplitude-amplify toward the strongest — Kannaka's recall, run as interference on a quantum computer. |

## Install

```bash
pip install -e .          # from this directory
```

Authentication uses a qBraid API key, resolved from (in order): `QBRAID_API_KEY`, a saved `~/.qbraid/qbraidrc` (`QbraidProvider(api_key=...).save_config()`), or `~/Downloads/QBraid.txt`.

## CLI

```bash
kannaka-quantum devices --online
kannaka-quantum run --qasm-file bell.qasm --shots 200
kannaka-quantum qrng --bits 16
kannaka-quantum recall --amplitudes 0.1,0.9,0.2,0.15 --labels alpha,beta,gamma,delta
```

Every command prints one JSON object (errors included).

## MCP server

```bash
kannaka-quantum mcp        # stdio transport
```

Register with Claude Code:

```bash
claude mcp add kannaka-quantum -- python -m kannaka_quantum mcp
```

…then any agent can call `quantum_devices`, `run_circuit`, `quantum_random`, and `resonance_recall`.

## Example: resonance recall

```text
$ kannaka-quantum recall --amplitudes 0.1,0.9,0.2,0.15 --labels alpha,beta,gamma,delta
{"distribution": {"alpha": 2, "beta": 775, "gamma": 240, "delta": 7},
 "quantum_top": "beta", "classical_top": "beta", "agree": true,
 "qubits": 2, "candidates": 4, "amplified": true,
 "device": "qbraid:qbraid:sim:qir-sv"}
```

Amplitude amplification sharpens the prepared resonance state toward the strongest memory — the recall ran on a quantum computer, and it agrees with the classical argmax.

## License

MIT.
