"""MCP server exposing Kannaka's quantum capabilities to any MCP client
(Claude Code, the harness, other agents). Tools map 1:1 to ``core``.

Run with: ``python -m kannaka_quantum mcp``  (stdio transport)
"""

from __future__ import annotations

from typing import Optional

from mcp.server.fastmcp import FastMCP

from . import core

mcp = FastMCP("kannaka-quantum")


@mcp.tool()
def quantum_devices(online_only: bool = False) -> list[dict]:
    """List quantum devices across providers (qBraid + OpenQuantum) with status,
    qubit counts, and cost.

    Use this to discover what's available before running a circuit. The free
    ``qbraid:qbraid:sim:qir-sv`` simulator (30 qubits) needs no credits.
    ``openquantum:*`` entries are real QPUs (IonQ/Rigetti/IQM/AQT) that spend
    Spark credits — there is no free OpenQuantum simulator.
    """
    return core.list_devices(online_only=online_only)


@mcp.tool()
def run_circuit(
    qasm3: str,
    shots: int = 100,
    device: str = core.DEFAULT_DEVICE,
    allow_spend: bool = False,
    max_credits: Optional[float] = None,
) -> dict:
    """Execute an OpenQASM 3 program on a backend and return measurement counts.

    `qasm3` is OpenQASM 3.0 source (include "stdgates.inc"; declare qubit[]/bit[],
    apply gates, measure). Defaults to the free qBraid simulator. To run on a real
    OpenQuantum QPU pass e.g. ``device="openquantum:iqm:garnet"`` AND
    ``allow_spend=True`` (it spends real Spark credits; a per-shot cost cap of
    ``max_credits`` credits applies, default 1.0 ≈ $2).
    """
    return core.run_qasm(qasm3, device=device, shots=shots, allow_spend=allow_spend, max_credits=max_credits)


@mcp.tool()
def quantum_random(n_bits: int = 8, device: str = core.DEFAULT_DEVICE, allow_spend: bool = False) -> dict:
    """Generate true quantum random bits from measurement collapse (not a PRNG).

    Defaults to the free qBraid simulator. Pass ``device="openquantum:<backend>"``
    + ``allow_spend=True`` to draw entropy from a real QPU (spends Spark credits).
    Returns the bitstring, its integer value, and a float in [0,1).
    """
    return core.qrng(n_bits, device=device, allow_spend=allow_spend)


@mcp.tool()
def resonance_recall(
    amplitudes: list[float],
    labels: Optional[list[str]] = None,
    shots: int = 1024,
    amplify: bool = True,
    device: str = core.DEFAULT_DEVICE,
    allow_spend: bool = False,
    max_credits: Optional[float] = None,
) -> dict:
    """Run Kannaka's resonance recall *as a quantum circuit*.

    Amplitude-encodes the candidate memory resonances into a quantum state and
    (with `amplify`) runs amplitude amplification toward the strongest — the
    quantum analogue of "attention as gravity." Returns the measured
    distribution over candidates plus the quantum vs classical top pick.

    Defaults to the free qBraid simulator. For the real-hardware artifact pass
    ``device="openquantum:iqm:garnet"`` + ``allow_spend=True`` — note recall
    defaults to 1024 shots, so keep shots low on paid QPUs (the ``max_credits``
    cap, default 1.0, guards against an accidental budget-draining run).
    """
    return core.quantum_recall(
        amplitudes,
        labels=labels,
        shots=shots,
        amplify=amplify,
        device=device,
        allow_spend=allow_spend,
        max_credits=max_credits,
    )


def run_stdio() -> None:
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    run_stdio()
