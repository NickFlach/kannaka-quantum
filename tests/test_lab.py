"""Network-free tests for the qBraid Lab bridge logic (rate math, spend guard,
CLI arg parsing). The live API path is exercised by hand against the account.
"""

import pytest

from kannaka_quantum import lab
from kannaka_quantum.cli import _parse_json_arg


def test_rate_to_credits_per_min():
    # 1 credit = $0.01. $0.24/hour = $0.004/min = 0.4 credits/min.
    assert lab._rate_to_credits_per_min(0.24, "hour") == 0.4
    assert lab._rate_to_credits_per_min(0.002, "min") == 0.2
    assert lab._rate_to_credits_per_min(8.74, "hour") == pytest.approx(14.5667, abs=1e-3)
    assert lab._rate_to_credits_per_min(None, "hour") is None
    # Unknown time frame is read conservatively as per-minute.
    assert lab._rate_to_credits_per_min(0.05, "") == 5.0


def test_parse_json_arg():
    assert _parse_json_arg(None) is None
    assert _parse_json_arg("") is None
    assert _parse_json_arg("numpy,scipy") == ["numpy", "scipy"]
    assert _parse_json_arg('["a", "b"]') == ["a", "b"]
    assert _parse_json_arg('{"numpy": "1.26"}') == {"numpy": "1.26"}


class _FakeProfile:
    pricing = None
    rate_dollar = 0.24
    rate_time_frame = "hour"


class _FakeClient:
    def __init__(self, balance):
        self._balance = balance

    def user_credits_value(self):
        return self._balance

    def get_profile(self, slug):
        return _FakeProfile()


def test_spend_guard_refuses_without_opt_in(monkeypatch):
    monkeypatch.delenv("KANNAKA_LAB_ALLOW_SPEND", raising=False)
    with pytest.raises(RuntimeError, match="allow_spend"):
        lab._compute_spend_guard(_FakeClient(1000), allow_spend=False, max_credits=10, profile_slug="x")


def test_spend_guard_does_not_honor_circuit_env_var(monkeypatch):
    # The circuit-shot opt-in must NOT unlock per-minute Lab compute.
    monkeypatch.delenv("KANNAKA_LAB_ALLOW_SPEND", raising=False)
    monkeypatch.setenv("KANNAKA_QUANTUM_ALLOW_SPEND", "1")
    with pytest.raises(RuntimeError, match="allow_spend"):
        lab._compute_spend_guard(_FakeClient(1000), allow_spend=False, max_credits=10, profile_slug="x")


def test_spend_guard_refuses_when_balance_cannot_cover_a_minute(monkeypatch):
    monkeypatch.delenv("KANNAKA_LAB_ALLOW_SPEND", raising=False)
    # Fake rate 0.4 cr/min; balance below one minute of burn → refuse.
    with pytest.raises(RuntimeError, match="cannot cover even one minute"):
        lab._compute_spend_guard(_FakeClient(0.1), allow_spend=True, max_credits=60, profile_slug="x")


def test_spend_guard_allows_affordable_launch_below_cap(monkeypatch):
    monkeypatch.delenv("KANNAKA_LAB_ALLOW_SPEND", raising=False)
    # balance (5) < max_credits (60) but well above the per-minute rate → allowed.
    g = lab._compute_spend_guard(_FakeClient(5), allow_spend=True, max_credits=60, profile_slug="x")
    assert g["max_credits_committed"] == 60
    # runway reflects min(cap, balance) / rate = 5 / 0.4 = 12.5 min.
    assert g["runway_minutes"] == 12.5


def test_spend_guard_rejects_nonpositive_cap(monkeypatch):
    monkeypatch.delenv("KANNAKA_LAB_ALLOW_SPEND", raising=False)
    with pytest.raises(RuntimeError, match="must be a positive"):
        lab._compute_spend_guard(_FakeClient(1000), allow_spend=True, max_credits=0, profile_slug="x")
    with pytest.raises(RuntimeError, match="must be a positive"):
        lab._compute_spend_guard(_FakeClient(1000), allow_spend=True, max_credits=-5, profile_slug="x")


def test_spend_guard_passes_and_reports_runway(monkeypatch):
    monkeypatch.delenv("KANNAKA_LAB_ALLOW_SPEND", raising=False)
    g = lab._compute_spend_guard(_FakeClient(1000), allow_spend=True, max_credits=40, profile_slug="x")
    # Fake profile rate: $0.24/hour → 0.4 cr/min; runway = min(40, 1000) / 0.4 = 100 min.
    assert g["credits_per_min"] == 0.4
    assert g["runway_minutes"] == 100.0
    assert g["max_credits_committed"] == 40
    assert g["balance_credits"] == 1000


def test_spend_guard_env_opt_in(monkeypatch):
    monkeypatch.setenv("KANNAKA_LAB_ALLOW_SPEND", "1")
    # Opt-in via the lab env var works; rate passed directly.
    g = lab._compute_spend_guard(_FakeClient(1000), allow_spend=False, max_credits=20, rate=2.0)
    assert g["credits_per_min"] == 2.0
    assert g["runway_minutes"] == 10.0
