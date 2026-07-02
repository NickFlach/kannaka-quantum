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


# ── T4.1: instance leases + reap + unleased-launch refusal ──────────────────


@pytest.fixture
def _isolated_leases(tmp_path, monkeypatch):
    monkeypatch.setenv("KANNAKA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KANNAKA_LAB_ALLOW_SPEND", raising=False)
    return tmp_path


class _FakeComputeClient:
    def __init__(self):
        self.stopped_instances = []
        self.stopped_servers = []

    def user_credits_value(self):
        return 1000.0

    def get_profile(self, slug):
        return _FakeProfile()

    def provision_bma_instance(self, profile):
        class _Inst:
            instance_id = "i-abc123"
            stopped_credits_per_min = 0.1
        return _Inst()

    def stop_bma_instance(self, instance_id):
        self.stopped_instances.append(instance_id)
        return {"stopped": instance_id}

    def stop_server(self, cluster_id=None):
        self.stopped_servers.append(cluster_id)
        return {"stopped_server": cluster_id}


def test_provision_records_lease(_isolated_leases, monkeypatch):
    from qbraid_core.services.compute import ComputeClient

    fake = _FakeComputeClient()
    monkeypatch.setattr(lab, "_client", lambda cls: fake)
    monkeypatch.setattr(ComputeClient, "bma_ssh_alias", staticmethod(lambda iid: f"alias-{iid}"))

    out = lab.lab_provision_instance("gpu-x", allow_spend=True, max_minutes=30)
    lease = out["lease"]
    assert lease["instance_id"] == "i-abc123"
    assert lease["kind"] == "instance"
    assert lease["max_minutes"] == 30
    assert lease["ssh_alias"] == "alias-i-abc123"
    assert lease["status"] == "active"
    # Persisted and readable back.
    assert lab._read_leases()["i-abc123"]["status"] == "active"


def test_reap_stops_expired_not_fresh(_isolated_leases, monkeypatch):
    fake = _FakeComputeClient()
    monkeypatch.setattr(lab, "_client", lambda cls: fake)
    lab._append_lease({"instance_id": "i-exp", "kind": "instance", "ssh_alias": "a-exp",
                       "status": "active", "expires_at": "2000-01-01T00:00:00Z", "event": "provision"})
    lab._append_lease({"instance_id": "i-fresh", "kind": "instance", "ssh_alias": "a-fresh",
                       "status": "active", "expires_at": "2999-01-01T00:00:00Z", "event": "provision"})

    out = lab.lab_reap()
    assert fake.stopped_instances == ["i-exp"]
    assert out["reaped_count"] == 1
    leases = lab._read_leases()
    assert leases["i-exp"]["status"] == "reaped"
    assert leases["i-fresh"]["status"] == "active"


def test_reap_stops_expired_server(_isolated_leases, monkeypatch):
    fake = _FakeComputeClient()
    monkeypatch.setattr(lab, "_client", lambda cls: fake)
    lab._append_lease({"instance_id": "server:default", "kind": "server", "cluster": None,
                       "status": "active", "expires_at": "2000-01-01T00:00:00Z", "event": "compute_up"})

    lab.lab_reap()
    assert fake.stopped_servers == [None]
    assert lab._read_leases()["server:default"]["status"] == "reaped"


def test_reap_dry_run_stops_nothing(_isolated_leases, monkeypatch):
    # dry-run must not even construct a client.
    monkeypatch.setattr(lab, "_client", lambda cls: (_ for _ in ()).throw(AssertionError("client built in dry-run")))
    lab._append_lease({"instance_id": "i-exp", "kind": "instance", "status": "active",
                       "expires_at": "2000-01-01T00:00:00Z", "event": "provision"})
    out = lab.lab_reap(dry_run=True)
    assert out["reaped_count"] == 1
    assert out["reaped"][0]["would_stop"] is True
    assert lab._read_leases()["i-exp"]["status"] == "active"  # untouched


class _FakeLauncher:
    def __init__(self):
        self.launched = []

    def remote_launch(self, ssh_alias, tool, **kw):
        self.launched.append(ssh_alias)

        class _S:
            session_id = "sess-1"
            tool = "claude"
            status = "running"
        return _S()


def test_agent_launch_refuses_unleased(_isolated_leases, monkeypatch):
    launcher = _FakeLauncher()
    monkeypatch.setattr(lab, "_agent_launcher", lambda: launcher)
    with pytest.raises(RuntimeError, match="no active lease"):
        lab.lab_agent_launch("alias-unleased")
    assert launcher.launched == []  # never reached the launcher


def test_agent_launch_allows_leased(_isolated_leases, monkeypatch):
    launcher = _FakeLauncher()
    monkeypatch.setattr(lab, "_agent_launcher", lambda: launcher)
    lab._append_lease({"instance_id": "i-1", "kind": "instance", "ssh_alias": "alias-leased",
                       "status": "active", "expires_at": "2999-01-01T00:00:00Z", "event": "provision"})
    out = lab.lab_agent_launch("alias-leased")
    assert out["launched"] is True
    assert launcher.launched == ["alias-leased"]


def test_agent_launch_allow_unleased_override(_isolated_leases, monkeypatch):
    launcher = _FakeLauncher()
    monkeypatch.setattr(lab, "_agent_launcher", lambda: launcher)
    out = lab.lab_agent_launch("alias-unleased", allow_unleased=True)
    assert out["launched"] is True
    assert launcher.launched == ["alias-unleased"]


# --------------------------------------------------------------------------- #
# lab_exec / lab_qos_boot (remote layer stubbed at _remote_ssh_sh)
# --------------------------------------------------------------------------- #
class _FakeSh:
    """Record commands sent through _remote_ssh_sh and script the replies.

    Replies are matched by substring against the remote command; first match
    wins. Unmatched commands succeed with empty output.
    """

    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or []

    def __call__(self, ssh_alias, command, timeout=90, stdin=""):
        import subprocess

        self.calls.append((ssh_alias, command, timeout))
        for needle, rc, out, err in self.replies:
            if needle in command:
                return subprocess.CompletedProcess([], rc, stdout=out, stderr=err)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")


def test_exec_returns_rc_and_output_without_raising(monkeypatch):
    sh = _FakeSh([("false-cmd", 1, "partial", "boom")])
    monkeypatch.setattr(lab, "_remote_ssh_sh", sh)
    out = lab.lab_exec("alias-x", "false-cmd --now")
    assert out["ok"] is False and out["rc"] == 1
    assert out["stdout"] == "partial" and out["stderr"] == "boom"
    assert out["truncated"] is False
    assert sh.calls[0][0] == "alias-x"


def test_exec_caps_output_to_tail(monkeypatch):
    big = "x" * (lab.EXEC_OUTPUT_CAP + 100) + "THE-END"
    sh = _FakeSh([("big", 0, big, "")])
    monkeypatch.setattr(lab, "_remote_ssh_sh", sh)
    out = lab.lab_exec("alias-x", "big")
    assert out["truncated"] is True
    assert out["stdout"].endswith("THE-END")  # tail, not head
    assert len(out["stdout"]) == lab.EXEC_OUTPUT_CAP


def test_exec_refuses_empty_command(monkeypatch):
    monkeypatch.setattr(lab, "_remote_ssh_sh", _FakeSh())
    with pytest.raises(RuntimeError, match="empty command"):
        lab.lab_exec("alias-x", "   ")


def test_qos_boot_refuses_unleased(_isolated_leases, monkeypatch):
    sh = _FakeSh()
    monkeypatch.setattr(lab, "_remote_ssh_sh", sh)
    with pytest.raises(RuntimeError, match="no active lease"):
        lab.lab_qos_boot("alias-unleased")
    assert sh.calls == []  # never touched the instance


def test_qos_boot_happy_path_reports_ready(_isolated_leases, monkeypatch):
    lab._append_lease({"instance_id": "i-1", "kind": "instance", "ssh_alias": "alias-qos",
                       "status": "active", "expires_at": "2999-01-01T00:00:00Z", "event": "provision"})
    sh = _FakeSh([
        ("has-session", 1, "", ""),  # no existing session
        ("tmux new-session", 0, "[BOOT] QuantumOS ready\nTimer tick 1 received\n", ""),
    ])
    monkeypatch.setattr(lab, "_remote_ssh_sh", sh)
    out = lab.lab_qos_boot("alias-qos")
    assert out["booted"] is True and out["already_running"] is False
    assert out["session"] == "qos"
    assert "tmux attach -t qos" in out["attach"]
    # prep ran before boot and cloned the default repo
    prep = next(c for _, c, _ in sh.calls if "apt-get" in c)
    assert "flaukowski/QuantumOS" in prep and "make -C" in prep


def test_qos_boot_existing_session_reported_not_clobbered(_isolated_leases, monkeypatch):
    lab._append_lease({"instance_id": "i-1", "kind": "instance", "ssh_alias": "alias-qos",
                       "status": "active", "expires_at": "2999-01-01T00:00:00Z", "event": "provision"})
    sh = _FakeSh([
        ("has-session", 0, "", ""),
        ("capture-pane", 0, "Timer tick 42 received\n", ""),
    ])
    monkeypatch.setattr(lab, "_remote_ssh_sh", sh)
    out = lab.lab_qos_boot("alias-qos")
    assert out["already_running"] is True
    assert not any("kill-session" in c for _, c, _ in sh.calls)
    assert not any("apt-get" in c for _, c, _ in sh.calls)  # no rebuild


def test_qos_boot_fresh_kills_and_reboots(_isolated_leases, monkeypatch):
    lab._append_lease({"instance_id": "i-1", "kind": "instance", "ssh_alias": "alias-qos",
                       "status": "active", "expires_at": "2999-01-01T00:00:00Z", "event": "provision"})
    sh = _FakeSh([
        ("has-session", 0, "", ""),
        ("tmux new-session", 0, "[BOOT] QuantumOS ready\n", ""),
    ])
    monkeypatch.setattr(lab, "_remote_ssh_sh", sh)
    out = lab.lab_qos_boot("alias-qos", fresh=True)
    assert out["booted"] is True
    assert any("kill-session" in c for _, c, _ in sh.calls)


def test_qos_boot_build_failure_raises(_isolated_leases, monkeypatch):
    lab._append_lease({"instance_id": "i-1", "kind": "instance", "ssh_alias": "alias-qos",
                       "status": "active", "expires_at": "2999-01-01T00:00:00Z", "event": "provision"})
    sh = _FakeSh([
        ("has-session", 1, "", ""),
        ("apt-get", 2, "", "make: *** [kernel] Error 2"),
    ])
    monkeypatch.setattr(lab, "_remote_ssh_sh", sh)
    with pytest.raises(RuntimeError, match="prep/build failed"):
        lab.lab_qos_boot("alias-qos")


def test_qos_boot_rejects_hostile_session_name(_isolated_leases, monkeypatch):
    monkeypatch.setattr(lab, "_remote_ssh_sh", _FakeSh())
    with pytest.raises(RuntimeError, match="invalid tmux session"):
        lab.lab_qos_boot("alias-qos", session="qos; rm -rf /", allow_unleased=True)
