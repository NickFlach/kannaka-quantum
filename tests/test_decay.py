"""decay (ADR-0002) — protocol logic at $0 through an injected runner."""
import math

import pytest

from kannaka_quantum import decay


def test_align_to_sequencer_clock():
    assert decay.align(0) == 0.0
    assert abs(decay.align(100e-6) - 100e-6) < 1e-12
    assert abs(decay.align(1e-8) - 0.0) < 1e-15  # below half a tick rounds to zero
    d = decay.align(5.01e-6)
    assert abs((d / decay.CLOCK_S) - round(d / decay.CLOCK_S)) < 1e-6


def test_programs_are_native_quil_t():
    t1 = decay.program("t1", 200e-6)
    assert t1.splitlines() == ["DECLARE ro BIT[1]", "RX(pi) 0", "DELAY 0 0.0002", "MEASURE 0 ro[0]"]
    assert "DELAY" not in decay.program("t1", 0.0)
    r = decay.program("ramsey", 10e-6, qubit=3)
    # 10 us is not a multiple of the 32 ns clock: the emitted duration is the aligned one
    assert r.count("RX(pi/2) 3") == 2 and f"DELAY 3 {decay._fmt(decay.align(10e-6))}" in r
    assert "0.000009984" in r
    e = decay.program("echo", 10e-6)
    assert e.count(f"DELAY 0 {decay._fmt(decay.align(5e-6))}") == 2 and "RX(pi) 0" in e
    with pytest.raises(ValueError):
        decay.program("t2star", 1e-6)
    for arm in decay.ARMS:  # native gate set only
        for line in decay.program(arm, 20e-6).splitlines():
            assert line.split()[0] in ("DECLARE", "RX(pi)", "RX(pi/2)", "DELAY", "MEASURE"), line


def test_p_one_and_half_life():
    assert decay.p_one({"0": 10, "1": 90}) == 0.9
    assert math.isnan(decay.p_one({}))
    assert decay.half_life_us([(0, 0.9), (10, 0.5), (20, 0.1)]) == 10.0
    assert decay.half_life_us([(0, 0.5), (10, 0.5)]) is None


def _fake_runner(t1_us=30.0, floor=0.1, cost_per_job=7.0):
    """A qubit with T1 = t1_us; ramsey dephases twice as fast; echo recovers half of that."""
    def runner(quil: str, shots: int):
        d = 0.0
        for line in quil.splitlines():
            if line.startswith("DELAY"):
                d += float(line.split()[2]) * 1e6
        if "RX(pi) 0\nDELAY" in quil or quil.count("RX(pi)") == 1 and "RX(pi/2)" not in quil:
            p1 = floor + (1 - 2 * floor) * math.exp(-d / t1_us)      # t1 arm
        elif quil.count("RX(pi/2)") == 2 and "RX(pi) 0" in quil:
            p1 = floor + (0.5 - floor) * (1 - math.exp(-d / (t1_us * 1.0)))  # echo: rises from ~floor
        else:
            p1 = 0.5 + (0.5 - floor) * math.exp(-d / (t1_us / 2))     # ramsey: falls from ~1 to 1/2
        ones = round(p1 * shots)
        return {"counts": {"1": ones, "0": shots - ones}, "billed": {"cost": cost_per_job,
                "timeStamps": {"executionDuration": 30 + d}}, "job_id": f"fake-{d}"}
    return runner


def test_run_decay_full_protocol():
    said = []
    rec = decay.run_decay(_fake_runner(), delays_us=(0, 10, 30, 100), shots=1000, log=said.append)
    assert rec["status"] == "complete"
    assert rec["abort_check"]["aborted"] is False
    # abort check ran first, on t1, at 0 and the longest delay
    assert [(j["arm"], j["delay_us"]) for j in rec["jobs"][:2]] == [("t1", 0.0), ("t1", 100.0)]
    # every (arm, delay) measured exactly once
    assert len(rec["jobs"]) == 3 * 4
    t1_hl = rec["curves"]["t1"]["half_life_us"]
    assert t1_hl is not None and 15 < t1_hl < 45, t1_hl  # ~ T1 ln2 ≈ 21 us on a 4-point grid
    assert rec["credits_total"] == 12 * 7.0


def test_run_decay_aborts_when_delays_are_ignored():
    def flat(quil, shots):
        return {"counts": {"1": int(0.9 * shots), "0": int(0.1 * shots)}, "billed": {"cost": 7.0}}
    rec = decay.run_decay(flat, delays_us=(0, 50, 100), shots=100)
    assert rec["status"] == "aborted-delays-not-executed"
    assert len(rec["jobs"]) == 2 and rec["credits_total"] == 14.0


def test_run_decay_stops_at_credit_cap():
    rec = decay.run_decay(_fake_runner(cost_per_job=10.0), delays_us=(0, 10, 30, 100), shots=100, max_credits_total=45)
    assert rec["status"].startswith("credit cap")
    assert rec["credits_total"] <= 50  # the job that crossed the line is the last one


def test_ledger_row_and_save(tmp_path):
    rec = decay.run_decay(_fake_runner(), delays_us=(0, 10, 100), shots=200)
    (tmp_path / "LEDGER.md").write_text("| date | what | device | jobs | abort Δ | half-lives | cost | status |\n")
    p = decay.save(rec, tmp_path, "rigetti:rigetti:qpu:cepheus-1-108q")
    assert p.exists() and p.name.startswith("decay-")
    rows = (tmp_path / "LEDGER.md").read_text(encoding="utf-8").splitlines()
    assert rows[-1].startswith("| ") and "decay (ADR-0002)" in rows[-1] and "complete" in rows[-1]
