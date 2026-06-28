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

#: Devices whose id starts with this route to OpenQuantum (Quantum Rings) instead
#: of qBraid. Form: ``openquantum:<backend short_code>`` e.g. ``openquantum:iqm:garnet``.
OPENQUANTUM_PREFIX = "openquantum:"

#: OpenQuantum has NO free simulator — every job spends real "Spark" credits
#: (1 credit = $2; the free tier is 25 credits / $50 per 90 days). These are the
#: documented public-compute per-shot USD prices, used for a *pre-flight* cost
#: estimate so a careless large-shot run on a pricey QPU can't silently drain the
#: budget (e.g. 1024 shots on IonQ ≈ $49 = the whole free tier). The real charge
#: is the device's live quote; this table is the guard rail, not the invoice.
OQ_USD_PER_SHOT = {
    "ionq:forte-1": 0.04800,
    "aqt:ibex-q1": 0.01410,
    "iqm:emerald": 0.00096,
    "iqm:garnet": 0.00087,
    "rigetti:cepheus-1-108q": 0.000255,
}
OQ_USD_PER_CREDIT = 2.0
#: Default ceiling for a single OpenQuantum run, in credits (≈ $2). Raise per-call
#: (``max_credits`` / ``--max-credits``) or via ``OPENQUANTUM_MAX_CREDITS``.
OQ_DEFAULT_MAX_CREDITS = 1.0
#: Required workload tag on every OpenQuantum job. Override with the subcategory
#: arg / ``OPENQUANTUM_SUBCATEGORY``; we learn the canonical value from the first
#: real submission's error if this default is rejected.
OQ_DEFAULT_SUBCATEGORY = "phys:oth"

#: qBraid credits are $0.01 each; real QPUs expose live per-task/per-shot/per-minute
#: pricing in device metadata. Default ceiling 200 credits (≈ $2), matching the
#: OpenQuantum default. Per-minute-billed devices (e.g. native Rigetti at 12000
#: credits/min ≈ $120/min) are refused outright — their cost can't be bounded from
#: a shot count.
QBRAID_USD_PER_CREDIT = 0.01
QBRAID_DEFAULT_MAX_CREDITS = 200.0


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


# ---------------------------------------------------------------------------
# OpenQuantum (Quantum Rings) — a second backend provider. Real QPUs (IonQ,
# Rigetti, IQM, AQT) behind an OAuth2 client-credentials API. No free simulator,
# so every run is gated behind an explicit spend opt-in + a credit ceiling.
# ---------------------------------------------------------------------------


def _oq_credentials():
    """Resolve OpenQuantum SDK credentials (client_id + client_secret).

    Order: ``OPENQUANTUM_CLIENT_ID``/``_SECRET`` env, then a JSON sdk-key at
    ``OPENQUANTUM_SDK_KEY``, then ``~/.openquantum/sdk-key.json``, then — a
    workstation convenience — the downloaded ``~/Downloads/sdk-key-*.json``.
    Returns a ``ClientCredentials`` or ``None``.
    """
    import json

    from openquantum_sdk.auth import ClientCredentials

    cid = os.environ.get("OPENQUANTUM_CLIENT_ID")
    csec = os.environ.get("OPENQUANTUM_CLIENT_SECRET")
    if cid and csec:
        return ClientCredentials(cid.strip(), csec.strip())

    candidates = [os.environ.get("OPENQUANTUM_SDK_KEY"), str(Path.home() / ".openquantum" / "sdk-key.json")]
    candidates += [str(p) for p in sorted((Path.home() / "Downloads").glob("sdk-key-*.json"))]
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand)
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        if d.get("client_id") and d.get("client_secret"):
            return ClientCredentials(d["client_id"], d["client_secret"])
    return None


def _oq_clients():
    """A (SchedulerClient, ManagementClient) pair sharing one auth/token."""
    from openquantum_sdk import ManagementClient, SchedulerClient
    from openquantum_sdk.clients import ClientCredentialsAuth

    creds = _oq_credentials()
    if creds is None:
        raise RuntimeError(
            "OpenQuantum credentials not found. Set OPENQUANTUM_CLIENT_ID/"
            "OPENQUANTUM_CLIENT_SECRET, or OPENQUANTUM_SDK_KEY to the sdk-key JSON "
            "path, or place it at ~/.openquantum/sdk-key.json."
        )
    auth = ClientCredentialsAuth(creds)
    mgmt = ManagementClient(auth=auth)
    sched = SchedulerClient(auth=auth, management_client=mgmt)
    return sched, mgmt


def _oq_page(paginated, *attrs) -> list:
    """Pull the item list out of an SDK paginated response across field names."""
    for a in attrs:
        v = getattr(paginated, a, None)
        if v is not None:
            return list(v)
    try:
        return list(paginated)
    except Exception:
        return []


def _oq_backends(mgmt) -> list:
    return _oq_page(mgmt.list_backend_classes(limit=50), "backend_classes", "data", "items", "results")


def _oq_org_id(mgmt) -> Optional[str]:
    orgs = _oq_page(mgmt.list_user_organizations(limit=20), "organizations", "data", "items")
    return getattr(orgs[0], "id", None) if orgs else None


def _oq_backend_code(b) -> Optional[str]:
    """The short backend id (e.g. ``iqm:garnet``) used in device strings."""
    return getattr(b, "short_code", None) or getattr(b, "name", None) or getattr(b, "id", None)


def _oq_estimate_cost(device: str, shots: int, max_credits: Optional[float], allow_spend: bool) -> Optional[dict]:
    """Gate an OpenQuantum run on (1) an explicit spend opt-in and (2) a credit
    ceiling, using the documented per-shot price as a pre-flight estimate.

    Raises ``RuntimeError`` if not opted in or if the estimate exceeds the cap.
    Returns the estimate dict (or ``None`` for an unpriced device the caller
    explicitly accepted via ``max_credits``).
    """
    if not (allow_spend or os.environ.get("KANNAKA_QUANTUM_ALLOW_SPEND") == "1"):
        raise RuntimeError(
            "OpenQuantum runs spend real Spark credits — there is no free simulator. "
            "Re-run with allow_spend=True (CLI: --allow-spend) or set "
            "KANNAKA_QUANTUM_ALLOW_SPEND=1. Use the free qBraid simulator "
            f"({DEFAULT_DEVICE}) for $0 testing."
        )
    cap = max_credits if max_credits is not None else float(os.environ.get("OPENQUANTUM_MAX_CREDITS", OQ_DEFAULT_MAX_CREDITS))
    code = device[len(OPENQUANTUM_PREFIX):]
    price = OQ_USD_PER_SHOT.get(code)
    if price is None:
        if max_credits is None:
            raise RuntimeError(
                f"unknown per-shot price for '{device}' — pass max_credits=<credits> to "
                "acknowledge a run whose cost can't be pre-estimated."
            )
        return None  # caller accepted an unpriced device by giving an explicit cap
    est_usd = price * int(shots)
    est_credits = est_usd / OQ_USD_PER_CREDIT
    if est_credits > cap:
        raise RuntimeError(
            f"estimated {est_credits:.3f} credits (${est_usd:.2f}) for {shots} shots on "
            f"{device} exceeds the {cap}-credit cap — lower shots or raise max_credits."
        )
    return {"per_shot_usd": price, "est_usd": round(est_usd, 4), "est_credits": round(est_credits, 4)}


def _oq_counts(output: Any) -> dict[str, int]:
    """Best-effort {bitstring: count} extraction from download_job_output."""
    if isinstance(output, dict):
        for key in ("counts", "measurement_counts", "histogram", "meas"):
            c = output.get(key)
            if isinstance(c, dict) and c:
                return {str(k): int(v) for k, v in c.items()}
        # A bare {bitstring: int} dict.
        if output and all(isinstance(v, int) for v in output.values()):
            return {str(k): int(v) for k, v in output.items()}
    for getter in (lambda o: o.get_counts(), lambda o: o.counts, lambda o: o.data.meas.get_counts()):
        try:
            c = getter(output)
            if c:
                return {str(k): int(v) for k, v in dict(c).items()}
        except Exception:
            continue
    return {}


def _run_openquantum(
    qasm: str,
    device: str,
    shots: int,
    allow_spend: bool = False,
    max_credits: Optional[float] = None,
    subcategory: Optional[str] = None,
) -> dict[str, Any]:
    """Submit an OpenQASM program to an OpenQuantum QPU and return counts.

    Spends real Spark credits — gated by :func:`_oq_estimate_cost`.
    """
    from openquantum_sdk.clients import JobSubmissionConfig

    estimate = _oq_estimate_cost(device, shots, max_credits, allow_spend)
    sched, mgmt = _oq_clients()
    backend_class_id = device[len(OPENQUANTUM_PREFIX):]
    subcat = subcategory or os.environ.get("OPENQUANTUM_SUBCATEGORY") or OQ_DEFAULT_SUBCATEGORY
    cfg = JobSubmissionConfig(
        backend_class_id=backend_class_id,
        name="kannaka-quantum",
        job_subcategory_id=subcat,
        shots=int(shots),
        organization_id=_oq_org_id(mgmt),
        auto_approve_quote=True,
    )
    job = sched.submit_job(cfg, file_content=qasm.encode("utf-8"))
    output = sched.download_job_output(job)
    counts = _oq_counts(output)
    result = {
        "device": device,
        "shots": shots,
        "job_id": getattr(job, "id", None),
        "counts": counts,
        "provider": "openquantum",
        "backend": backend_class_id,
        "cost_estimate": estimate,
    }
    if not counts:  # surface the raw shape so we can tighten _oq_counts after a first run
        result["raw_output"] = str(output)[:2000]
    return result


def list_devices(online_only: bool = False, include_openquantum: bool = True) -> list[dict[str, Any]]:
    """List quantum devices across providers with status and qubit counts.

    qBraid's ``qbraid:qbraid:sim:qir-sv`` simulator is free (no credits).
    OpenQuantum entries (``openquantum:*``) are real QPUs that spend Spark
    credits — there is no free OpenQuantum simulator.
    """
    devices: list[dict[str, Any]] = []

    # qBraid fleet (free simulator + QPUs). Wrapped so an OpenQuantum-only setup
    # (or a missing qBraid key) still returns a usable list.
    try:
        provider = _provider()
        for dev in provider.get_devices():
            try:
                md = dev.metadata()
            except Exception:  # pragma: no cover - network/SDK variance
                md = {}
            did = md.get("device_id") or getattr(dev, "id", None)
            status = str(md.get("status") or "")
            is_sim = "sim" in str(did).lower()
            devices.append(
                {
                    "id": did,
                    "qubits": md.get("num_qubits"),
                    "status": status.split(".")[-1] or "UNKNOWN",
                    "simulator": is_sim,
                    "provider": str(did).split(":")[1] if did and ":" in did else None,
                    "cost": "free" if is_sim else "qbraid-credits",
                }
            )
    except Exception as e:  # pragma: no cover - report but don't fail the listing
        devices.append({"id": None, "provider": "qbraid", "status": "ERROR", "error": str(e)})

    # OpenQuantum fleet (real QPUs; spends Spark credits). Best-effort; skipped
    # silently when no OpenQuantum credentials are configured.
    if include_openquantum and _oq_credentials() is not None:
        try:
            _, mgmt = _oq_clients()
            for b in _oq_backends(mgmt):
                code = _oq_backend_code(b)
                status = str(getattr(b, "status", "") or "")
                accepting = getattr(b, "accepting_jobs", None)
                price = OQ_USD_PER_SHOT.get(code or "")
                devices.append(
                    {
                        "id": f"{OPENQUANTUM_PREFIX}{code}",
                        "qubits": getattr(b, "num_qubits", None) or getattr(b, "qubits", None),
                        # Normalize to qBraid's casing so the online_only filter is uniform.
                        "status": (status.split(".")[-1] or ("ONLINE" if accepting else "UNKNOWN")).upper(),
                        "simulator": False,
                        "provider": "openquantum",
                        "cost": (f"${price}/shot" if price is not None else "spark-credits"),
                    }
                )
        except Exception as e:  # pragma: no cover
            devices.append({"id": None, "provider": "openquantum", "status": "ERROR", "error": str(e)})

    if online_only:
        devices = [d for d in devices if d.get("status") == "ONLINE"]
    devices.sort(key=lambda d: (not d.get("simulator"), d.get("provider") or "", d.get("id") or ""))
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


def _qbraid_spend_guard(
    pricing: dict, device: str, shots: int, allow_spend: bool, max_credits: Optional[float]
) -> dict[str, Any]:
    """Gate a real (non-simulator) qBraid run on an explicit spend opt-in + a
    credit ceiling, using the device's live pricing metadata.

    Refuses per-minute-billed devices outright (cost unbounded by shot count —
    this is the native-Rigetti $120/min trap). Raises on opt-out / over-cap;
    returns the estimate otherwise.
    """
    if not (allow_spend or os.environ.get("KANNAKA_QUANTUM_ALLOW_SPEND") == "1"):
        raise RuntimeError(
            f"{device} is a real qBraid QPU and spends qBraid credits. Re-run with "
            f"allow_spend=True (CLI: --allow-spend) or set KANNAKA_QUANTUM_ALLOW_SPEND=1. "
            f"Use the free simulator ({DEFAULT_DEVICE}) for $0 testing."
        )
    per_task = float(pricing.get("perTask") or 0.0)
    per_shot = float(pricing.get("perShot") or 0.0)
    per_min = float(pricing.get("perMinute") or 0.0)
    if per_min > 0:
        raise RuntimeError(
            f"{device} bills per-minute ({per_min:g} credits/min ≈ ${per_min * QBRAID_USD_PER_CREDIT:.0f}/min) — "
            "cost cannot be bounded from a shot count, so it is refused. Choose a per-shot device "
            "(e.g. aws:rigetti:qpu:cepheus-1-108q or aws:ionq:qpu:forte-1)."
        )
    cap = max_credits if max_credits is not None else float(
        os.environ.get("QBRAID_MAX_CREDITS", QBRAID_DEFAULT_MAX_CREDITS)
    )
    est_credits = per_task + per_shot * int(shots)
    est_usd = est_credits * QBRAID_USD_PER_CREDIT
    if est_credits > cap:
        raise RuntimeError(
            f"estimated {est_credits:.2f} qBraid credits (${est_usd:.2f}) for {shots} shots on "
            f"{device} exceeds the {cap}-credit cap — lower shots or raise max_credits."
        )
    return {
        "per_task_credits": per_task,
        "per_shot_credits": per_shot,
        "est_credits": round(est_credits, 3),
        "est_usd": round(est_usd, 4),
    }


def run_qasm(
    qasm3: str,
    device: str = DEFAULT_DEVICE,
    shots: int = 100,
    allow_spend: bool = False,
    max_credits: Optional[float] = None,
    subcategory: Optional[str] = None,
) -> dict[str, Any]:
    """Run an OpenQASM program on a device and return measurement counts.

    Routes to OpenQuantum (real QPUs, spends Spark credits — gated) when
    ``device`` starts with ``openquantum:``; otherwise runs on qBraid (the free
    simulator by default).
    """
    if device.startswith(OPENQUANTUM_PREFIX):
        return _run_openquantum(
            qasm3, device, shots, allow_spend=allow_spend, max_credits=max_credits, subcategory=subcategory
        )
    provider = _provider()
    dev = provider.get_device(device)
    cost_estimate = None
    if "sim" not in device.lower():  # real qBraid QPU — gate the spend
        try:
            pricing = (dev.metadata() or {}).get("pricing") or {}
        except Exception:
            pricing = {}
        cost_estimate = _qbraid_spend_guard(pricing, device, shots, allow_spend, max_credits)
    job = dev.run(qasm3, shots=shots)
    try:
        job.wait_for_final_state(timeout=300)
    except Exception:
        pass
    res = job.result()
    out: dict[str, Any] = {
        "device": device,
        "shots": shots,
        "job_id": getattr(job, "id", None),
        "counts": _counts_from_result(res),
    }
    if cost_estimate is not None:
        out["cost_estimate"] = cost_estimate
    return out


def run_qiskit(
    circuit,
    device: str = DEFAULT_DEVICE,
    shots: int = 100,
    allow_spend: bool = False,
    max_credits: Optional[float] = None,
    subcategory: Optional[str] = None,
) -> dict[str, Any]:
    """Run a Qiskit circuit (serialized to OpenQASM 3) on a device."""
    from qiskit.qasm3 import dumps

    return run_qasm(
        dumps(circuit),
        device=device,
        shots=shots,
        allow_spend=allow_spend,
        max_credits=max_credits,
        subcategory=subcategory,
    )


def qrng(
    n_bits: int = 8,
    device: str = DEFAULT_DEVICE,
    allow_spend: bool = False,
    max_credits: Optional[float] = None,
    subcategory: Optional[str] = None,
) -> dict[str, Any]:
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
    out = run_qiskit(
        qc, device=device, shots=shots, allow_spend=allow_spend, max_credits=max_credits, subcategory=subcategory
    )
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
    allow_spend: bool = False,
    max_credits: Optional[float] = None,
    subcategory: Optional[str] = None,
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
    out = run_qiskit(
        qc, device=device, shots=shots, allow_spend=allow_spend, max_credits=max_credits, subcategory=subcategory
    )

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
