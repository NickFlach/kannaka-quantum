"""Security-surface tests for the remote-agent path (ssh_bridge + lab).

All offline: subprocess / SSH calls are faked. These lock in the mitigations
documented in docs/adr-0001-remote-agent-surface.md so a regression can't
silently re-expose a secret in argv or drop host-key pinning.
"""

import json

import pytest

from kannaka_quantum import lab, ssh_bridge


class _FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_remote_ssh_py_pins_host_key_and_sends_secret_via_stdin(monkeypatch, tmp_path):
    monkeypatch.setenv("KANNAKA_DATA_DIR", str(tmp_path))
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        return _FakeCompleted(stdout='{"ok": true}')

    monkeypatch.setattr(lab.subprocess, "run", fake_run)

    out = lab._remote_ssh_py("kannaka-instance-xyz", "print('hi')", stdin="SECRET-PAYLOAD")
    assert out == '{"ok": true}'

    argv = captured["argv"]
    # Host-key verification is restored (qBraid's alias config disables it).
    assert "StrictHostKeyChecking=accept-new" in argv
    assert "HostKeyAlias=kannaka-instance-xyz" in argv
    khf = [a for a in argv if a.startswith("UserKnownHostsFile=")]
    assert khf and str(tmp_path) in khf[0]

    # The payload (which carries the API key in real use) travels over stdin,
    # never argv — so it can't leak into the local process list.
    assert captured["kwargs"]["input"] == "SECRET-PAYLOAD"
    assert not any("SECRET-PAYLOAD" in str(a) for a in argv)


def test_resolve_bridge_token_precedence(monkeypatch):
    monkeypatch.delenv("KANNAKA_SSH_BRIDGE_TOKEN", raising=False)
    # Explicit token wins; nothing → None.
    assert ssh_bridge._resolve_bridge_token("qbr-explicit") == "qbr-explicit"
    assert ssh_bridge._resolve_bridge_token(None) is None
    # Env fallback keeps the token out of argv for direct invocations.
    monkeypatch.setenv("KANNAKA_SSH_BRIDGE_TOKEN", "  qbr-from-env  ")
    assert ssh_bridge._resolve_bridge_token(None) == "qbr-from-env"
    assert ssh_bridge._resolve_bridge_token("qbr-explicit") == "qbr-explicit"


def test_agent_setup_key_via_stdin_never_in_argv_or_result(monkeypatch):
    captured = {}

    def fake_remote(ssh_alias, script, stdin=""):
        captured.update(ssh_alias=ssh_alias, script=script, stdin=stdin)
        return '{"apiKeyHelper": "cat /home/u/.claude/anthropic_key", "model": "claude-x"}'

    monkeypatch.setattr(lab, "_remote_ssh_py", fake_remote)

    secret = "sk-ant-SECRETVALUE-123"
    res = lab.lab_agent_setup("kannaka-instance-xyz", api_key=secret)

    # Key is delivered over stdin, not baked into the remote command (argv)…
    assert secret in captured["stdin"]
    assert secret not in captured["script"]
    assert captured["script"] == lab._AGENT_SETUP_SCRIPT
    # …and is never echoed back in the tool's JSON result.
    assert secret not in json.dumps(res)
    assert res["configured"] is True


def test_agent_setup_rejects_non_anthropic_provider(monkeypatch):
    # Guardrail: only the audited Anthropic path is wired; other providers must
    # not silently fall through to uploading a key.
    with pytest.raises(RuntimeError, match="Anthropic"):
        lab.lab_agent_setup("alias", provider="openai", api_key="x")


def test_resolve_provider_key_prefers_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-env")
    assert lab._resolve_provider_key("anthropic") == "sk-ant-env"
    assert lab._resolve_provider_key("openai") == "sk-oai-env"


# ── T4.2: scoped per-instance keys — blast-radius mitigation (ADR-0001) ──────


def _fake_remote(store):
    def f(ssh_alias, script, stdin=""):
        store.update(alias=ssh_alias, script=script, stdin=stdin)
        return '{"removed": ["/home/u/.claude/anthropic_key"]}'
    return f


def test_agent_setup_refuses_primary_key(tmp_path, monkeypatch):
    monkeypatch.setenv("KANNAKA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-PRIMARY")
    store = {}
    monkeypatch.setattr(lab, "_remote_ssh_py", _fake_remote(store))
    with pytest.raises(RuntimeError, match="PRIMARY"):
        lab.lab_agent_setup("alias-x", api_key="sk-ant-PRIMARY")
    assert store == {}  # refused before any remote upload


def test_agent_setup_primary_key_override_i_know(tmp_path, monkeypatch):
    monkeypatch.setenv("KANNAKA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-PRIMARY")
    store = {}
    monkeypatch.setattr(lab, "_remote_ssh_py", _fake_remote(store))
    out = lab.lab_agent_setup("alias-x", api_key="sk-ant-PRIMARY", i_know=True)
    assert out["configured"] is True
    assert "sk-ant-PRIMARY" in store["stdin"]  # delivered via stdin, not argv
    assert out["key_fingerprint"].startswith("sha256:")


def test_agent_setup_scoped_key_records_fingerprint_never_raw(tmp_path, monkeypatch):
    monkeypatch.setenv("KANNAKA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-PRIMARY")
    store = {}
    monkeypatch.setattr(lab, "_remote_ssh_py", _fake_remote(store))
    secret = "sk-ant-SCOPED-per-instance-XYZ"
    out = lab.lab_agent_setup("alias-x", api_key=secret)
    fp = lab._key_fingerprint(secret)
    assert out["key_fingerprint"] == fp
    leases_text = lab._leases_path().read_text()
    assert fp in leases_text          # fingerprint recorded against the instance
    assert secret not in leases_text  # the raw key is NEVER persisted


def test_agent_teardown_removes_remote_key_and_reminds(tmp_path, monkeypatch):
    monkeypatch.setenv("KANNAKA_DATA_DIR", str(tmp_path))
    lab._append_lease({"instance_id": "i-1", "ssh_alias": "alias-x", "status": "active",
                       "expires_at": "2999-01-01T00:00:00Z", "key_fingerprint": "sha256:deadbeef0000",
                       "event": "provision"})
    store = {}
    monkeypatch.setattr(lab, "_remote_ssh_py", _fake_remote(store))
    out = lab.lab_agent_teardown("alias-x")
    assert out["torn_down"] is True
    assert out["removed_key_fingerprint"] == "sha256:deadbeef0000"
    assert "rotate" in out["rotation_reminder"].lower()
    assert "anthropic_key" in store["script"]  # the teardown script targets the key file
    # Fingerprint cleared from the lease after teardown.
    assert lab._read_leases()["i-1"]["key_fingerprint"] is None
