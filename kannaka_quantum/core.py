"""Core quantum operations for Kannaka, executed on real quantum backends via
qBraid.

Kannaka's memory is a *Holographic Resonance Medium*: recall is wave
interference, and "attention acts as gravity — wavefronts whose phase/amplitude
align with the query are pulled forward." That is, almost verbatim, the
definition of quantum amplitude amplification. This module makes the
correspondence literal:

- ``run_qasm`` / ``run_qiskit`` — execute arbitrary circuits on qBraid devices
  (free simulator by default; real QPUs when the account has credits).
- ``qrng`` — true quantum random bits (the medium's irrationality, Ξ, drawn
  from measurement collapse rather than a PRNG).
- ``quantum_recall`` — amplitude-encode a set of memory resonances into a
  quantum state and (optionally) amplitude-amplify toward the strongest, so
  recall is performed *by interference* on a quantum computer.

Auth: a qBraid API key from ``QBRAID_API_KEY``, the saved ``~/.qbraid/qbraidrc``
(``QbraidProvider.save_config()``), or — as a convenience on this workstation —
``~/Downloads/QBraid.txt``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

#: qBraid's native state-vector simulator — free, 30 qubits, no credits needed.
DEFAULT_DEVICE = "qbraid:qbraid:sim:qir-sv"
SIM_QUBIT_CAP = 28


def _resolve_api_key() -> Optional[str]:
    key = os.environ.get("QBRAID_API_KEY")
    if key:
        return key.strip()
    # Fallback for this workstation's setup.
    dl = Path.home() / "Downloads" / "QBraid.txt"
    if dl.exists():
        m = re.search(r"qbr_[A-Za-z0-9_\-]+", dl.read_text())
        if m:
            return m.group(0)
    return None


def _provider():
    from qbraid.runtime import QbraidProvider

    key = _resolve_api_key()
    # If a key is found we pass it; otherwise rely on a saved qbraidrc.
    return QbraidProvider(api_key=key) if key else QbraidProvider()


def list_devices(online_only: bool = False) -> list[dict[str, Any]]:
    """List qBraid devices (QPUs + simulators) with status and qubit counts."""
    provider = _provider()
    devices = []
    for dev in provider.get_devices():
        try:
            md = dev.metadata()
        except Exception:  # pragma: no cover - network/SDK variance
            md = {}
        did = md.get("device_id") or getattr(dev, "id", None)
        status = str(md.get("status") or "")
        devices.append(
            {
                "id": did,
                "qubits": md.get("num_qubits"),
                "status": status.split(".")[-1] or "UNKNOWN",
                "simulator": "sim" in str(did).lower(),
                "provider": str(did).split(":")[1] if did and ":" in did else None,
            }
        )
    if online_only:
        devices = [d for d in devices if d["status"] == "ONLINE"]
    devices.sort(key=lambda d: (not d["simulator"], d["id"] or ""))
    return devices


def _counts_from_result(res: Any) -> dict[str, int]:
    """Pull a {bitstring: count} dict out of a qBraid Result across SDK shapes."""
    for getter in (
        lambda r: r.data.get_counts(),
        lambda r: r.measurement_counts(),
        lambda r: r.get_counts(),
        lambda r: r.data.measurement_counts,
    ):
        try:
            c = getter(res)
            if c:
                return {str(k): int(v) for k, v in dict(c).items()}
        except Exception:
            continue
    return {}


def run_qasm(qasm3: str, device: str = DEFAULT_DEVICE, shots: int = 100) -> dict[str, Any]:
    """Run an OpenQASM 3 program on a qBraid device and return measurement counts."""
    provider = _provider()
    dev = provider.get_device(device)
    job = dev.run(qasm3, shots=shots)
    try:
        job.wait_for_final_state(timeout=300)
    except Exception:
        pass
    res = job.result()
    return {
        "device": device,
        "shots": shots,
        "job_id": getattr(job, "id", None),
        "counts": _counts_from_result(res),
    }


def run_qiskit(circuit, device: str = DEFAULT_DEVICE, shots: int = 100) -> dict[str, Any]:
    """Run a Qiskit circuit (transpiled to OpenQASM 3) on a qBraid device."""
    from qiskit.qasm3 import dumps

    return run_qasm(dumps(circuit), device=device, shots=shots)


def qrng(n_bits: int = 8, device: str = DEFAULT_DEVICE) -> dict[str, Any]:
    """Generate ``n_bits`` of true quantum randomness from measurement collapse.

    Runs an ``H^⊗k`` circuit and reads measured bitstrings (one per shot),
    concatenating until ``n_bits`` are produced. Returns the bits, their integer
    value, and a [0,1) float — a drop-in quantum entropy source for the medium's
    irrationality / dream noise.
    """
    from qiskit import QuantumCircuit

    n_bits = max(1, int(n_bits))
    width = min(n_bits, SIM_QUBIT_CAP)
    shots = (n_bits + width - 1) // width
    qc = QuantumCircuit(width, width)
    qc.h(range(width))
    qc.measure(range(width), range(width))
    out = run_qiskit(qc, device=device, shots=shots)
    # Expand counts into a flat list of per-shot bitstrings (order-independent;
    # fine for entropy). qBraid returns big-endian bitstrings.
    samples: list[str] = []
    for bits, c in out["counts"].items():
        samples.extend([bits.zfill(width)] * int(c))
    if not samples:
        raise RuntimeError("no measurements returned from device")
    bitstr = "".join(samples)[:n_bits]
    value = int(bitstr, 2) if bitstr else 0
    return {
        "bits": bitstr,
        "n_bits": n_bits,
        "int": value,
        "float": value / (2 ** n_bits),
        "device": device,
        "job_id": out["job_id"],
    }


def _optimal_iterations(target_amplitude: float) -> int:
    """Optimal amplitude-amplification iterations for a target that starts with
    amplitude ``a`` (sin θ = a) in the prepared state.

    Each Grover iteration rotates the state by 2θ toward the target, so the
    angle reaches π/2 (probability 1) after m = (π/2 − θ)/(2θ) iterations.
    Unlike the textbook ``(π/4)√N`` (which assumes a *uniform* start), this
    accounts for amplitude-encoded resonances — where the top memory may
    already start near the top, so the right answer is often 0 or 1 iterations.
    Over-rotating past π/2 would *de*-amplify the target.
    """
    a = float(min(1.0, max(0.0, target_amplitude)))
    if a <= 1e-9:
        return 0
    theta = float(np.arcsin(a))
    if theta >= np.pi / 2:
        return 0
    return max(0, int(round((np.pi / 2 - theta) / (2 * theta))))


def quantum_recall(
    amplitudes: Sequence[float],
    labels: Optional[Sequence[str]] = None,
    shots: int = 1024,
    amplify: bool = True,
    iterations: Optional[int] = None,
    device: str = DEFAULT_DEVICE,
) -> dict[str, Any]:
    """Perform Kannaka's resonance recall *as a quantum circuit*.

    The candidate memory resonances are amplitude-encoded into a quantum state
    ``|ψ⟩ = Σ (aᵢ/‖a‖)|i⟩`` (the query's interference pattern over the medium).
    Measuring already samples memories in proportion to ``aᵢ²``. With
    ``amplify=True`` we run amplitude amplification *about the prepared state*
    toward the strongest resonance — sharpening the recall by interference, the
    quantum analogue of "attention as gravity."

    Returns the measured distribution over candidates, the quantum top pick, and
    the classical argmax for comparison (they should agree — the point is that
    the recall ran on a quantum computer).
    """
    a = np.clip(np.asarray(amplitudes, dtype=float), 0.0, None)
    k = len(a)
    if k == 0:
        raise ValueError("need at least one amplitude")
    if labels is not None and len(labels) != k:
        raise ValueError("labels must match amplitudes length")
    n = max(1, int(np.ceil(np.log2(k))))
    dim = 2 ** n
    if n > SIM_QUBIT_CAP:
        raise ValueError(f"{k} candidates need {n} qubits (> {SIM_QUBIT_CAP} cap)")

    vec = np.zeros(dim)
    vec[:k] = a
    if not np.any(vec):
        vec[:k] = 1.0
    vec = vec / np.linalg.norm(vec)
    classical_top = int(np.argmax(a))

    from qiskit import QuantumCircuit
    from qiskit.circuit.library import StatePreparation

    prep = StatePreparation(vec)
    qc = QuantumCircuit(n, n)
    qc.append(prep, range(n))

    iters = 0
    if amplify and k > 1:
        if iterations is not None:
            iters = max(0, min(int(iterations), 8))
        else:
            iters = min(_optimal_iterations(vec[classical_top]), 8)
    if iters > 0:
        # LSB-first bit order so _phase_flip targets the qiskit basis state
        # ``classical_top`` (qubit q holds bit q), matching StatePreparation.
        target_bits = format(classical_top, f"0{n}b")[::-1]
        for _ in range(iters):
            # Oracle: phase-flip the strongest-resonance basis state.
            _phase_flip(qc, target_bits)
            # Diffuser about |ψ⟩:  A (2|0><0| - I) A†.
            qc.append(prep.inverse(), range(n))
            _phase_flip(qc, "0" * n)
            qc.append(prep, range(n))

    qc.measure(range(n), range(n))
    out = run_qiskit(qc, device=device, shots=shots)

    dist: dict[int, int] = {}
    for bits, c in out["counts"].items():
        # qBraid returns big-endian bitstrings; reverse to qiskit's
        # little-endian index (StatePreparation / measurement convention).
        idx = int(bits[::-1], 2)
        if idx < k:
            dist[idx] = dist.get(idx, 0) + int(c)
    quantum_top = max(dist, key=dist.get) if dist else None

    def lbl(i: Optional[int]):
        if i is None:
            return None
        return labels[i] if labels is not None else i

    return {
        "distribution": {str(lbl(i)): v for i, v in sorted(dist.items())},
        "quantum_top": lbl(quantum_top),
        "classical_top": lbl(classical_top),
        "agree": quantum_top == classical_top,
        "qubits": n,
        "candidates": k,
        "amplified": iters > 0,
        "iterations": iters,
        "device": device,
        "shots": shots,
        "job_id": out["job_id"],
    }


def _phase_flip(qc, bitstring: str) -> None:
    """Append a phase flip (Z) on the basis state ``bitstring`` (big-endian)."""
    n = len(bitstring)
    # X-mask the 0 bits so the all-ones controlled-Z targets ``bitstring``.
    zeros = [i for i, b in enumerate(bitstring) if b == "0"]
    for i in zeros:
        qc.x(i)
    if n == 1:
        qc.z(0)
    else:
        qc.h(n - 1)
        qc.mcx(list(range(n - 1)), n - 1)
        qc.h(n - 1)
    for i in zeros:
        qc.x(i)
