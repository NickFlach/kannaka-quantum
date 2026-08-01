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
    lab_agent_launch,
    lab_agent_list,
    lab_agent_read,
    lab_agent_send,
    lab_agent_setup,
    lab_compute_down,
    lab_compute_status,
    lab_compute_up,
    lab_compute_usage,
    lab_create_env,
    lab_credits,
    lab_delete_env,
    lab_env_info,
    lab_exec,
    lab_list_envs,
    lab_list_instances,
    lab_list_profiles,
    lab_provision_instance,
    lab_qos_boot,
    lab_ssh_configure,
    lab_start_instance,
    lab_stop_instance,
)
from .qos_bridge import (
    qos_bridge_relay,
    qos_bridge_verify,
    verify_attestation,
)

__all__ = [
    "DEFAULT_DEVICE",
    "lab_agent_launch",
    "lab_agent_list",
    "lab_agent_read",
    "lab_agent_send",
    "lab_agent_setup",
    "lab_compute_down",
    "lab_compute_status",
    "lab_compute_up",
    "lab_compute_usage",
    "lab_create_env",
    # qBraid Lab / infrastructure
    "lab_credits",
    "lab_delete_env",
    "lab_env_info",
    "lab_exec",
    "lab_list_envs",
    "lab_list_instances",
    "lab_list_profiles",
    "lab_provision_instance",
    "lab_qos_boot",
    "lab_ssh_configure",
    "lab_start_instance",
    "lab_stop_instance",
    "list_devices",
    # QuantumOS <-> NATS swarm bridge
    "qos_bridge_relay",
    "qos_bridge_verify",
    "qrng",
    "quantum_recall",
    "run_qasm",
    "run_qiskit",
    "verify_attestation",
]
__version__ = "0.2.10"
