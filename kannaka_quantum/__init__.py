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
from .lab import (
    lab_compute_down,
    lab_compute_status,
    lab_compute_up,
    lab_compute_usage,
    lab_create_env,
    lab_credits,
    lab_delete_env,
    lab_env_info,
    lab_list_envs,
    lab_list_instances,
    lab_list_profiles,
    lab_provision_instance,
    lab_start_instance,
    lab_stop_instance,
    lab_ssh_configure,
    lab_agent_launch,
    lab_agent_list,
    lab_agent_read,
    lab_agent_send,
)

__all__ = [
    "DEFAULT_DEVICE",
    "list_devices",
    "qrng",
    "quantum_recall",
    "run_qasm",
    "run_qiskit",
    # qBraid Lab / infrastructure
    "lab_credits",
    "lab_list_envs",
    "lab_env_info",
    "lab_list_profiles",
    "lab_compute_status",
    "lab_compute_usage",
    "lab_list_instances",
    "lab_create_env",
    "lab_delete_env",
    "lab_compute_up",
    "lab_compute_down",
    "lab_provision_instance",
    "lab_start_instance",
    "lab_stop_instance",
    "lab_ssh_configure",
    "lab_agent_launch",
    "lab_agent_list",
    "lab_agent_read",
    "lab_agent_send",
]
__version__ = "0.2.1"
