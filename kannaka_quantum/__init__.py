"""kannaka-quantum — give Kannaka real quantum capabilities via qBraid.

A CLI bridge (for the Kannaka coding agent) and an MCP server (for any agent),
wrapping qBraid execution with on-brand tools: run circuits, quantum RNG, and
amplitude-amplification "resonance recall".
"""

from .core import (
    DEFAULT_DEVICE,
    list_devices,
    qrng,
    quantum_recall,
    run_qasm,
    run_qiskit,
)

__all__ = [
    "DEFAULT_DEVICE",
    "list_devices",
    "qrng",
    "quantum_recall",
    "run_qasm",
    "run_qiskit",
]
__version__ = "0.1.0"
