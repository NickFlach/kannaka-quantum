"""Network-free tests for the quantum logic (verified locally via qiskit's
state-vector, so CI doesn't need a qBraid account). The end-to-end execution
path is exercised against the live free simulator by hand / the demo.
"""

import numpy as np
import pytest

from kannaka_quantum import core


def test_optimal_iterations_matches_grover_geometry():
    # Uniform start over 4 states: target amplitude 0.5 (θ=30°) → 1 iteration.
    assert core._optimal_iterations(0.5) == 1
    # Already dominant → don't rotate (would overshoot π/2 and de-amplify).
    assert core._optimal_iterations(0.95) == 0
    assert core._optimal_iterations(1.0) == 0
    assert core._optimal_iterations(0.0) == 0
    # Uniform over 16 (amp 0.25, θ≈14.5°): (π/2−θ)/(2θ) ≈ 2.6 → 3, matching
    # the textbook (π/4)√16 ≈ 3.14.
    assert core._optimal_iterations(0.25) == 3


@pytest.mark.parametrize("target", [0, 1, 2, 3])
def test_amplitude_amplification_marks_target_local(target):
    """Build prep(uniform) + one amplitude-amplification step toward ``target``
    and confirm (on a local state-vector) that the target basis state is the
    most probable — verifies _phase_flip's bit order and the diffuser."""
    from qiskit import QuantumCircuit
    from qiskit.circuit.library import StatePreparation
    from qiskit.quantum_info import Statevector

    n, dim = 2, 4
    vec = np.ones(dim) / np.sqrt(dim)
    prep = StatePreparation(vec)
    qc = QuantumCircuit(n)
    qc.append(prep, range(n))
    target_bits = format(target, f"0{n}b")[::-1]
    core._phase_flip(qc, target_bits)
    qc.append(prep.inverse(), range(n))
    core._phase_flip(qc, "0" * n)
    qc.append(prep, range(n))

    probs = Statevector(qc).probabilities()  # qiskit little-endian index
    assert int(np.argmax(probs)) == target
    assert probs[target] > 0.9  # one Grover step on a uniform 4-state ≈ certainty


def test_counts_parser_handles_shapes():
    class _D:
        def get_counts(self):
            return {"00": 5, "11": 7}

    class _R:
        data = _D()

    assert core._counts_from_result(_R()) == {"00": 5, "11": 7}
    assert core._counts_from_result(object()) == {}


def test_measured_index_device_aware():
    # qBraid-native backends report big-endian → reverse to the qiskit index.
    assert core._measured_index("10", "qbraid:qbraid:sim:qir-sv") == 1
    assert core._measured_index("01", "qbraid:qbraid:sim:qir-sv") == 2
    # AWS-routed devices (Rigetti via Braket) report the opposite order — no
    # reversal. Verified against a live aws:rigetti recall (2026-06-29): raw '01'
    # was the amplified target 'signal' (index 1), not the bit-reversed index 2.
    assert core._measured_index("10", "aws:rigetti:qpu:cepheus-1-108q") == 2
    assert core._measured_index("01", "aws:rigetti:qpu:cepheus-1-108q") == 1
