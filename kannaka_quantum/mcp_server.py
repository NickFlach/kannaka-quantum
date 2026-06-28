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
    """List qBraid quantum devices (QPUs + simulators) with status and qubit counts.

    Use this to discover what's available before running a circuit. The free
    ``qbraid:qbraid:sim:qir-sv`` simulator (30 qubits) needs no credits.
    """
    return core.list_devices(online_only=online_only)


@mcp.tool()
def run_circuit(qasm3: str, shots: int = 100, device: str = core.DEFAULT_DEVICE) -> dict:
    """Execute an OpenQASM 3 program on a qBraid backend and return measurement counts.

    `qasm3` is OpenQASM 3.0 source (include "stdgates.inc"; declare qubit[]/bit[],
    apply gates, measure). Defaults to the free simulator.
    """
    return core.run_qasm(qasm3, device=device, shots=shots)


@mcp.tool()
def quantum_random(n_bits: int = 8) -> dict:
    """Generate true quantum random bits from measurement collapse (not a PRNG).

    Returns the bitstring, its integer value, and a float in [0,1).
    """
    return core.qrng(n_bits)


@mcp.tool()
def resonance_recall(
    amplitudes: list[float],
    labels: Optional[list[str]] = None,
    shots: int = 1024,
    amplify: bool = True,
) -> dict:
    """Run Kannaka's resonance recall *as a quantum circuit*.

    Amplitude-encodes the candidate memory resonances into a quantum state and
    (with `amplify`) runs amplitude amplification toward the strongest — the
    quantum analogue of "attention as gravity." Returns the measured
    distribution over candidates plus the quantum vs classical top pick.
    """
    return core.quantum_recall(amplitudes, labels=labels, shots=shots, amplify=amplify)


def run_stdio() -> None:
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    run_stdio()
