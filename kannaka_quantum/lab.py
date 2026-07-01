"""qBraid Lab / infrastructure operations for the Kannaka coding agent.

Where :mod:`core` runs *circuits* on qBraid, this module manages the *platform*
around them — credits, environments, compute profiles, the Lab server, and
on-demand instances — via the installed ``qbraid_core`` client (the very same
account / ``~/.qbraid/qbraidrc`` the circuit bridge already authenticates with,
so no extra setup).

Every function returns a JSON-serializable dict; failures are raised and the
``cli`` / ``mcp`` layer renders them as ``{"error": ...}``. Read-only / $0 ops
are free. Anything that *starts paid compute* (``lab_compute_up``,
``lab_provision_instance``, ``lab_start_instance``) goes through
:func:`_compute_spend_guard`, mirroring core's spend guard: an explicit
``allow_spend`` opt-in plus a committed ``max_credits`` ceiling, checked against
the live balance. Compute bills **per wall-clock minute** and keeps charging
until stopped, so the guard surfaces the burn rate + runway rather than a fixed
invoice.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from .core import _resolve_api_key, QBRAID_USD_PER_CREDIT

#: Default ceiling (in qBraid credits) a single paid compute action may *risk*.
#: Compute is open-ended per-minute billing, so this is the balance the caller
#: commits to — not a hard auto-stop. ~$0.60; e.g. minutes on a GPU profile or
#: hours on a small CPU one. Override per-call (``max_credits``) or via the env
#: ``QBRAID_MAX_CREDITS``.
LAB_DEFAULT_MAX_CREDITS = 60.0


# --------------------------------------------------------------------------- #
# auth + small helpers
# --------------------------------------------------------------------------- #
def _client(cls):
    """Construct a ``qbraid_core`` service client, authenticating from the
    resolved key (``QBRAID_API_KEY`` / ``~/Downloads/QBraid.txt``) or, failing
    that, a saved ``qbraidrc``. Construction verifies the key against
    ``/users/verify`` (free — no credits)."""
    key = _resolve_api_key()
    return cls(api_key=key) if key else cls()


def _dump(obj: Any) -> Any:
    """Best-effort JSON-safe view of a pydantic model (or pass-through)."""
    md = getattr(obj, "model_dump", None)
    if callable(md):
        try:
            return md(mode="json")
        except Exception:
            try:
                return md()
            except Exception:
                pass
    return obj


def _rate_to_credits_per_min(rate_dollar: Optional[float], rate_time_frame: Optional[str]) -> Optional[float]:
    """Normalize a ``$rate / time_frame`` profile rate to qBraid credits/min."""
    if rate_dollar is None:
        return None
    tf = (rate_time_frame or "").lower()
    usd = float(rate_dollar)
    if "hour" in tf or tf in ("hr", "h"):
        usd_per_min = usd / 60.0
    elif "sec" in tf:
        usd_per_min = usd * 60.0
    else:  # "min"/"minute"/unknown → treat as per-minute (the conservative read)
        usd_per_min = usd
    return round(usd_per_min / QBRAID_USD_PER_CREDIT, 4)


def _profile_credits_per_min(p: Any) -> Optional[float]:
    return _rate_to_credits_per_min(getattr(p, "rate_dollar", None), getattr(p, "rate_time_frame", None))


def _envs():
    """Import the environments service, turning the (optional) jupyter_client
    dependency into a clear, actionable error instead of a raw ImportError."""
    try:
        from qbraid_core.services import environments as _e  # noqa: F401
        from qbraid_core.services.environments import EnvironmentManagerClient
        return EnvironmentManagerClient
    except ModuleNotFoundError as e:
        if "jupyter_client" in str(e):
            raise RuntimeError(
                "qBraid environment tools require 'jupyter_client' "
                "(pip install jupyter_client)."
            ) from e
        raise


# --------------------------------------------------------------------------- #
# Phase 1 — read-only / $0 visibility
# --------------------------------------------------------------------------- #
def lab_credits() -> dict[str, Any]:
    """Current qBraid credit balance (1 credit = $0.01)."""
    from qbraid_core import QbraidClientV1

    bal = _client(QbraidClientV1).get_credits_balance()
    credits = float(bal.get("qbraidCredits", 0.0) or 0.0)
    return {
        "qbraid_credits": round(credits, 4),
        "usd_value": round(credits * QBRAID_USD_PER_CREDIT, 2),
        "aws_credits": bal.get("awsCredits"),
        "auto_recharge": bal.get("autoRecharge"),
        "organization_id": bal.get("organizationId"),
    }


def lab_list_envs(page: int = 1, limit: int = 20) -> dict[str, Any]:
    """List qBraid environments available to the account."""
    client = _client(_envs())
    data = client.get_available_environments(page=page, limit=limit)
    if isinstance(data, dict):
        envs = data.get("environments", [])
        pagination = data.get("pagination")
    else:  # some versions return a bare list
        envs, pagination = list(data), None
    return {"environments": envs, "count": len(envs), "pagination": pagination}


def lab_env_info(slug: str) -> dict[str, Any]:
    """Metadata for one environment by slug."""
    client = _client(_envs())
    return {"slug": slug, "environment": client.get_environment_by_slug(slug)}


def lab_list_profiles(
    gpu_only: Optional[bool] = None,
    available_only: bool = False,
    plan: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    """List compute profiles (instance types) with live per-minute cost + GPU
    flag, so a caller can choose before spending."""
    from qbraid_core.services.compute import ComputeClient

    client = _client(ComputeClient)
    profs = client.list_ready_profiles() if available_only else client.list_profiles(plan=plan, gpu=gpu_only, limit=limit)
    out = []
    for p in profs:
        if gpu_only and not getattr(p, "gpu", False):
            continue
        if plan and getattr(p, "plan", None) != plan:
            continue  # list_ready_profiles() ignores the plan filter — apply it here
        if limit is not None and len(out) >= limit:
            break
        cpm = _profile_credits_per_min(p)
        out.append(
            {
                "slug": p.slug,
                "name": getattr(p, "display_name", None),
                "gpu": getattr(p, "gpu", False),
                "plan": getattr(p, "plan", None),
                "has_capacity": getattr(p, "has_capacity", None),
                "credits_per_min": cpm,
                "usd_per_min": round(cpm * QBRAID_USD_PER_CREDIT, 4) if cpm is not None else None,
                "rate": f"${getattr(p, 'rate_dollar', '?')}/{getattr(p, 'rate_time_frame', '?')}",
            }
        )
    return {"profiles": out, "count": len(out)}


def lab_compute_status(cluster: Optional[str] = None) -> dict[str, Any]:
    """Status of the user's Lab server (running? on which profile? at what URL?)."""
    from qbraid_core.services.compute import ComputeClient

    st = _client(ComputeClient).get_server_status(cluster_id=cluster)
    return {"server": _dump(st)}


def lab_compute_usage(days: Optional[int] = None) -> dict[str, Any]:
    """Compute usage / per-session credit rates + totals charged."""
    from qbraid_core.services.compute import ComputeClient

    return {"usage": _dump(_client(ComputeClient).get_usage(days=days))}


def lab_list_instances() -> dict[str, Any]:
    """List on-demand (BMA) compute instances and their per-minute credit burn."""
    from qbraid_core.services.compute import ComputeClient

    insts = _client(ComputeClient).list_bma_instances()
    out = []
    for i in insts:
        out.append(
            {
                "instance_id": getattr(i, "instance_id", None),
                "status": _dump(getattr(i, "status", None)),
                "profile_slug": getattr(i, "profile_slug", None),
                "url": getattr(i, "url", None),
                "ready": getattr(i, "ready", None),
                "running_credits_per_min": getattr(i, "running_credits_per_min", None),
                "stopped_credits_per_min": getattr(i, "stopped_credits_per_min", None),
            }
        )
    return {"instances": out, "count": len(out)}


def lab_list_kernels() -> dict[str, Any]:
    """List Jupyter kernels installed locally (meaningful inside Lab / on an
    instance where qBraid environments are installed)."""
    from qbraid_core.services.environments.kernels import get_all_kernels

    return {"kernels": _dump(get_all_kernels())}


# --------------------------------------------------------------------------- #
# Phase 2 — free mutations (env / package / kernel lifecycle; $0 but stateful)
# --------------------------------------------------------------------------- #
def lab_create_env(
    name: str,
    description: Optional[str] = None,
    python_version: Optional[str] = None,
    packages: Optional[Union[dict, Sequence[str]]] = None,
    kernel_name: Optional[str] = None,
    visibility: str = "private",
    tags: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Create a qBraid environment. Listed ``packages`` install during the
    (async, cloud-side) build — this is the reachable 'install packages into a
    qBraid environment' path when not running inside Lab."""
    from qbraid_core.services.environments.schema import EnvironmentConfig

    client = _client(_envs())
    # Accept packages as {name: version} or a bare list of names.
    pkgs: Optional[dict] = None
    if packages:
        pkgs = dict(packages) if isinstance(packages, dict) else {str(p): "" for p in packages}
    kwargs: dict[str, Any] = {"name": name, "visibility": visibility}
    if description is not None:
        kwargs["description"] = description
    if python_version is not None:
        kwargs["python_version"] = python_version
    if pkgs is not None:
        kwargs["python_packages"] = pkgs
    if kernel_name is not None:
        kwargs["kernel_name"] = kernel_name
    if tags is not None:
        kwargs["tags"] = list(tags)
    cfg = EnvironmentConfig(**kwargs)
    res = client.create_environment(cfg)
    return {
        "created": True,
        "name": name,
        "result": _dump(res),
        "note": "Build runs asynchronously on qBraid; any packages install during the build.",
    }


def lab_delete_env(slug: str) -> dict[str, Any]:
    """Delete a qBraid environment by slug."""
    client = _client(_envs())
    client.delete_environment(slug)
    return {"deleted": True, "slug": slug}


def lab_pip_install(env_id: str, packages: Sequence[str], upgrade_pip: bool = False) -> dict[str, Any]:
    """pip-install packages into a LOCALLY-installed environment, keyed by its
    local registry ``env_id`` (NOT the cloud slug). Only works on a machine
    where the env is installed — i.e. inside Lab / on a provisioned instance."""
    from qbraid_core.services.environments.packages import pip_install

    pkgs = list(packages)
    res = pip_install(env_id, pkgs, upgrade_pip=upgrade_pip)
    return {"installed": True, "env_id": env_id, "packages": pkgs, "result": _dump(res)}


def lab_pip_freeze(env_id: str) -> dict[str, Any]:
    """List installed packages in a locally-installed environment."""
    from qbraid_core.services.environments.packages import pip_freeze

    return {"env_id": env_id, "packages": _dump(pip_freeze(env_id))}


def lab_add_kernel(environment: str) -> dict[str, Any]:
    """Register a Jupyter kernel for a (locally-installed) environment."""
    from qbraid_core.services.environments.kernels import add_kernels

    add_kernels(environment)
    return {"added": True, "environment": environment}


def lab_remove_kernel(environment: str) -> dict[str, Any]:
    """Remove a Jupyter kernel for an environment."""
    from qbraid_core.services.environments.kernels import remove_kernels

    remove_kernels(environment)
    return {"removed": True, "environment": environment}


# --------------------------------------------------------------------------- #
# Phase 3 — paid compute provisioning (credit-spending; spend-gated)
# --------------------------------------------------------------------------- #
def _spend_opt_in(allow_spend: bool, what: str) -> None:
    # Deliberately a SEPARATE env var from core's KANNAKA_QUANTUM_ALLOW_SPEND:
    # a one-off circuit-shot opt-in must NOT silently authorize open-ended
    # per-minute Lab compute billing.
    if not (allow_spend or os.environ.get("KANNAKA_LAB_ALLOW_SPEND") == "1"):
        raise RuntimeError(
            f"{what} spends qBraid credits per wall-clock minute. Re-run with "
            "allow_spend=True (CLI: --allow-spend) and a max_credits ceiling, or "
            "set KANNAKA_LAB_ALLOW_SPEND=1 (note: this is SEPARATE from "
            "KANNAKA_QUANTUM_ALLOW_SPEND, which only unlocks circuit runs)."
        )


def _compute_spend_guard(
    client,
    allow_spend: bool,
    max_credits: Optional[float],
    *,
    profile_slug: Optional[str] = None,
    rate: Optional[float] = None,
    what: str = "Starting compute",
) -> dict[str, Any]:
    """Gate a paid compute action: explicit opt-in + a committed credit ceiling
    the live balance must cover. Returns the burn rate + runway (compute is
    open-ended per-minute, so this is a risk ceiling, not a fixed invoice)."""
    _spend_opt_in(allow_spend, what)
    # Resolve the authoritative per-minute credit rate.
    if rate is None and profile_slug is not None:
        try:
            det = client.get_profile(profile_slug)
            pricing = getattr(det, "pricing", None)
            if pricing is not None:
                rate = float(getattr(pricing, "credit_cost_per_minute", 0) or 0) or None
            if not rate:
                rate = _rate_to_credits_per_min(getattr(det, "rate_dollar", None), getattr(det, "rate_time_frame", None))
        except Exception:
            rate = None
    cap = max_credits if max_credits is not None else float(os.environ.get("QBRAID_MAX_CREDITS", LAB_DEFAULT_MAX_CREDITS))
    if cap <= 0:
        raise RuntimeError(
            f"max_credits must be a positive credit ceiling (got {cap}); set a real budget before starting paid compute."
        )
    balance = float(client.user_credits_value())
    # max_credits is a risk acknowledgement, not an enforced auto-stop, so don't
    # require the whole ceiling to be funded — refuse only when the balance can't
    # cover even a minimal runway (one minute of burn). With an unknown rate, fall
    # back to refusing an empty balance.
    floor = rate if rate else 0.0
    if balance <= floor:
        raise RuntimeError(
            f"balance {balance:.1f} credits (${balance * QBRAID_USD_PER_CREDIT:.2f}) cannot cover even one minute"
            + (f" at {rate:.2f} credits/min" if rate else "")
            + " — top up before launching paid compute."
        )
    effective_cap = min(cap, balance)
    runway = (effective_cap / rate) if rate else None
    return {
        "credits_per_min": rate,
        "usd_per_min": round(rate * QBRAID_USD_PER_CREDIT, 4) if rate else None,
        "max_credits_committed": cap,
        "balance_credits": round(balance, 2),
        "runway_minutes": round(runway, 1) if runway else None,
        "note": "Bills per wall-clock minute until you stop it (lab_compute_down / lab_stop_instance). "
        "max_credits is the balance you accept to risk, not an automatic cutoff; runway reflects min(max_credits, balance).",
    }


def lab_compute_up(
    profile: str,
    allow_spend: bool = False,
    max_credits: Optional[float] = None,
    cluster: Optional[str] = None,
    wait: bool = False,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    """Start the Lab server on a compute profile (PAID, per-minute)."""
    from qbraid_core.services.compute import ComputeClient

    client = _client(ComputeClient)
    guard = _compute_spend_guard(
        client, allow_spend, max_credits, profile_slug=profile, what=f"Starting the Lab server on '{profile}'"
    )
    res = client.start_server(profile, cluster_id=cluster)
    out = {"started": True, "profile": profile, "spend_guard": guard, "result": _dump(res)}
    if wait:
        # The server is already STARTED and billing; a wait failure must NOT
        # discard that fact (else the caller never learns to stop it).
        try:
            st = client.wait_for_server(timeout=timeout) if timeout is not None else client.wait_for_server()
            out["status"] = _dump(st)
        except Exception as e:
            out["wait_error"] = f"{type(e).__name__}: {e}"
            out["warning"] = (
                "The Lab server was STARTED and is billing per minute, but waiting for it to become "
                f"ready failed. It is NOT stopped — call lab_compute_down (cluster={cluster!r}) to stop "
                "billing, and lab_compute_status to check it."
            )
    return out


def lab_compute_down(cluster: Optional[str] = None) -> dict[str, Any]:
    """Stop the running Lab server (disk preserved; stops per-minute billing)."""
    from qbraid_core.services.compute import ComputeClient

    res = _client(ComputeClient).stop_server(cluster_id=cluster)
    return {"stopped": True, "result": _dump(res)}


def lab_provision_instance(
    profile: str,
    allow_spend: bool = False,
    max_credits: Optional[float] = None,
    wait: bool = False,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    """Provision (launch) a new on-demand compute instance (PAID, per-minute)."""
    from qbraid_core.services.compute import ComputeClient

    client = _client(ComputeClient)
    guard = _compute_spend_guard(
        client, allow_spend, max_credits, profile_slug=profile, what=f"Provisioning an instance on '{profile}'"
    )
    inst = client.provision_bma_instance(profile)
    instance_id = getattr(inst, "instance_id", None)
    out = {
        "provisioned": True,
        "profile": profile,
        "instance_id": instance_id,
        "spend_guard": guard,
        "instance": _dump(inst),
        # Surface the post-stop disk floor at PROVISION time (not just at stop):
        # the approving human should see that pausing still bills for disk.
        "stopped_credits_per_min": getattr(inst, "stopped_credits_per_min", None),
        "note": "Even after lab_stop_instance (pause), this instance keeps billing stopped_credits_per_min "
        "for its disk until terminated via the qBraid web UI (no agent terminate, by design).",
    }
    if wait and instance_id:
        try:
            out["instance"] = _dump(
                client.wait_for_bma_instance(instance_id, timeout=timeout) if timeout is not None else client.wait_for_bma_instance(instance_id)
            )
        except Exception as e:
            out["wait_error"] = f"{type(e).__name__}: {e}"
            out["warning"] = (
                f"Instance {instance_id} was PROVISIONED and is billing per minute, but waiting for it to "
                f"become ready failed. It is NOT stopped — call lab_stop_instance(instance_id={instance_id!r}) "
                "to stop running billing, and lab_list_instances to check it."
            )
    return out


def lab_start_instance(
    instance_id: str,
    allow_spend: bool = False,
    max_credits: Optional[float] = None,
) -> dict[str, Any]:
    """Resume a stopped on-demand instance (PAID, per-minute)."""
    from qbraid_core.services.compute import ComputeClient

    client = _client(ComputeClient)
    rate = None
    try:
        cur = client.get_bma_instance(instance_id)
        rate = getattr(cur, "running_credits_per_min", None)
    except Exception:
        rate = None
    guard = _compute_spend_guard(
        client, allow_spend, max_credits, rate=rate, what=f"Resuming instance '{instance_id}'"
    )
    inst = client.start_bma_instance(instance_id)
    return {"started": True, "instance_id": instance_id, "spend_guard": guard, "instance": _dump(inst)}


def lab_stop_instance(instance_id: str) -> dict[str, Any]:
    """Stop (pause) an on-demand instance — disk preserved, running billing
    stops. (A stopped instance still bills ``stopped_credits_per_min`` for disk;
    terminate-and-delete is left to the qBraid web UI by design.)"""
    from qbraid_core.services.compute import ComputeClient

    inst = _client(ComputeClient).stop_bma_instance(instance_id)
    return {
        "stopped": True,
        "instance_id": instance_id,
        "instance": _dump(inst),
        "note": "Disk preserved; a stopped instance still bills stopped_credits_per_min until terminated "
        "(terminate via the qBraid web UI to free the disk and stop all billing).",
    }


# --------------------------------------------------------------------------- #
# Phase 4 — remote agents (run a coding agent ON a provisioned instance via SSH)
# --------------------------------------------------------------------------- #
def _agent_launcher():
    """Construct an AgentLauncher for REMOTE (SSH) operations.

    qBraid's AgentLauncher.__init__ calls require_tmux(), but its remote_*
    methods only run tmux on the REMOTE host over SSH — so on a machine
    without local tmux (e.g. Windows) we bypass that local check for the
    remote path. (Local launch/list/send still need real tmux.)"""
    from qbraid_core.services.agents import launcher as _launcher

    orig = _launcher.require_tmux
    _launcher.require_tmux = lambda: None
    try:
        return _launcher.AgentLauncher()
    finally:
        _launcher.require_tmux = orig


def _agent_summary(s: Any) -> dict[str, Any]:
    sid = getattr(s, "session_id", None)
    if isinstance(sid, str):
        sid = sid.strip()
    return {
        "session_id": sid,
        "tool": getattr(s, "tool", None),
        "status": _dump(getattr(s, "status", None)),
        "cwd": getattr(s, "cwd", None),
        "model_name": getattr(s, "model_name", None),
        "cost_usd": getattr(s, "cost_usd", None),
        "total_tokens": getattr(s, "total_tokens", None),
        "agent_type": getattr(s, "agent_type", None),
        "last_activity": _dump(getattr(s, "last_activity", None)),
    }


def _harden_windows_ssh(cfg: dict) -> list[str]:
    """Make qBraid's generated SSH config usable on Windows. Two fixes, both
    verified necessary live: (1) qBraid's ProxyCommand bridge crashes under the
    Windows asyncio Proactor loop (connect_read_pipe(stdin) → WinError 6), so
    repoint it at our thread-based ssh-bridge shim; (2) Windows OpenSSH refuses
    config/key files that carry an inherited 'OWNER RIGHTS' ACE, so reset their
    ACLs to the current user only."""
    fixes: list[str] = []
    config_file = cfg.get("config_file")
    identity_file = cfg.get("identity_file")
    # 1. Repoint the ProxyCommand to our Windows-safe shim.
    if config_file and os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                text = f.read()
            new = text.replace(
                "-m qbraid_core.services.compute.ssh bridge",
                "-m kannaka_quantum ssh-bridge",
            )
            if new != text:
                with open(config_file, "w", encoding="utf-8") as f:
                    f.write(new)
                fixes.append("proxycommand→kannaka-ssh-bridge")
        except Exception as e:  # pragma: no cover
            fixes.append(f"proxycommand-rewrite-failed:{e}")
    # 2. Tighten ACLs (must run AFTER the rewrite, which can re-inherit them).
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "%USERNAME%"
    for path in (config_file, identity_file):
        if path and os.path.exists(path):
            try:
                subprocess.run(
                    ["icacls", path, "/inheritance:r", "/grant:r", f"{user}:F"],
                    capture_output=True, text=True, check=False,
                )
                fixes.append(f"acl:{os.path.basename(path)}")
            except Exception as e:  # pragma: no cover
                fixes.append(f"acl-failed:{e}")
    return fixes


def lab_ssh_configure(instance_id: str) -> dict[str, Any]:
    """Configure local SSH access to a running on-demand instance and return its
    stable SSH alias — the precursor to any lab_agent_* remote operation. On
    Windows it also applies the ssh-bridge + ACL fixes the remote path needs."""
    from qbraid_core.services.compute import ComputeClient

    client = _client(ComputeClient)
    cfg = client.configure_ssh_for_instance(instance_id)
    alias = ComputeClient.bma_ssh_alias(instance_id)
    cfg_dict = cfg if isinstance(cfg, dict) else _dump(cfg)
    out = {
        "instance_id": instance_id,
        "ssh_alias": alias,
        "config": cfg_dict,
        "note": "Use this ssh_alias for lab_agent_launch / lab_agent_list / lab_agent_read / lab_agent_send.",
    }
    if sys.platform == "win32" and isinstance(cfg_dict, dict):
        out["windows_fixes"] = _harden_windows_ssh(cfg_dict)
    return out


def lab_agent_launch(
    ssh_alias: str,
    tool: str = "claude",
    instructions: Optional[str] = None,
    cwd: Optional[str] = None,
    name: Optional[str] = None,
    agent_type: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Launch a coding agent (claude / codex / opencode) ON a remote provisioned
    instance over SSH — the kannaka agent driving another agent on cloud compute.
    Requires SSH already configured (lab_ssh_configure) and the instance running."""
    s = _agent_launcher().remote_launch(
        ssh_alias,
        tool,
        cwd=cwd,
        name=name,
        instructions=instructions,
        agent_type=agent_type,
        tags=list(tags) if tags else None,
    )
    out = _agent_summary(s)
    out["launched"] = True
    out["ssh_alias"] = ssh_alias
    return out


def lab_agent_list(ssh_alias: str, include_stopped: bool = False) -> dict[str, Any]:
    """List coding agents running on a remote instance."""
    sessions = _agent_launcher().remote_list(ssh_alias, include_stopped=include_stopped)
    return {"ssh_alias": ssh_alias, "agents": [_agent_summary(s) for s in sessions], "count": len(sessions)}


def lab_agent_read(ssh_alias: str, session_id: str, lines: int = 50) -> dict[str, Any]:
    """Read recent terminal output from a remote agent session."""
    out = _agent_launcher().remote_read(ssh_alias, session_id, lines=lines)
    return {"ssh_alias": ssh_alias, "session_id": session_id, "output": out}


def lab_agent_send(ssh_alias: str, session_id: str, text: str) -> dict[str, Any]:
    """Send a prompt/keystrokes to a remote agent session."""
    ok = _agent_launcher().remote_send(ssh_alias, session_id, text)
    return {"ssh_alias": ssh_alias, "session_id": session_id, "sent": bool(ok)}


def _known_hosts_path() -> str:
    """A persistent known_hosts under the kannaka data dir, for host-key pinning
    of remote instances (see :func:`_remote_ssh_py`)."""
    base = os.environ.get("KANNAKA_DATA_DIR") or str(Path.home() / ".kannaka")
    d = Path(base)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:  # pragma: no cover - falls through to ssh's own error
        pass
    return str(d / "known_hosts")


def _remote_ssh_py(ssh_alias: str, script: str, stdin: str = "") -> str:
    """Run a Python script on the remote instance over SSH, robustly.

    SSH flattens argv into a single remote-shell string, so a bare
    ``ssh alias python3 -c <multiline>`` gets re-split by the remote shell.
    Shell-quote the whole ``python3 -c ...`` so it arrives as one argument.
    The script reads any payload (e.g. the API key) from stdin, keeping secrets
    out of argv / the remote process list.

    Host-key handling: qBraid's generated alias config sets
    ``StrictHostKeyChecking no`` + ``UserKnownHostsFile /dev/null`` (ephemeral
    instances), so an instance's host key is never verified. For the one SSH
    command we fully control we override that to ``accept-new`` against a
    persistent known_hosts, keyed per-instance via ``HostKeyAlias`` so distinct
    instances don't collide: the key is pinned on first contact and a *changed*
    key for the same instance is refused (a swapped-instance / MITM signal).
    Transport is already TLS-authenticated by the wss:// ProxyCommand tunnel +
    qBraid token; this is defense-in-depth. See docs/adr-0001-remote-agent-surface.md."""
    remote = "python3 -c " + shlex.quote(script)
    r = subprocess.run(
        [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={_known_hosts_path()}",
            "-o", f"HostKeyAlias={ssh_alias}",
            ssh_alias,
            remote,
        ],
        input=stdin, capture_output=True, text=True, timeout=90,
    )
    out = (r.stdout or "").strip()
    if r.returncode != 0 and not out:
        raise RuntimeError(f"remote setup failed (rc={r.returncode}): {(r.stderr or '').strip()[:300]}")
    return out


def _resolve_provider_key(provider: str) -> Optional[str]:
    """Resolve an agent API key: env var → ~/.kannaka/config.toml [llm]."""
    provider = (provider or "anthropic").lower()
    env_name = "OPENAI_API_KEY" if provider in ("openai", "codex") else "ANTHROPIC_API_KEY"
    env = os.environ.get(env_name)
    if env:
        return env.strip()
    if provider in ("anthropic", "claude"):
        try:
            import tomllib

            cfg = tomllib.loads((Path.home() / ".kannaka" / "config.toml").read_text())
            llm = cfg.get("llm", {})
            if str(llm.get("provider", "")).lower() == "anthropic" and llm.get("api_key"):
                return str(llm["api_key"]).strip()
        except Exception:
            pass
    return None


#: Remote setup script — run on the instance to make a launched claude agent
#: fully autonomous (auth + skip onboarding + valid model). Reads {key, model}
#: from stdin. Verified live 2026-06-30.
_AGENT_SETUP_SCRIPT = r'''
import sys, json, pathlib, os
data = json.load(sys.stdin)
key = data["key"]; model = data.get("model")
home = pathlib.Path.home()
cdir = home / ".claude"; cdir.mkdir(parents=True, exist_ok=True)
# 1. API key via Claude Code's apiKeyHelper (survives qBraid's settings mgmt;
#    the bare env var never reaches qBraid's SSH-launched agent process).
kf = cdir / "anthropic_key"; kf.write_text(key); os.chmod(kf, 0o600)
sf = cdir / "settings.json"
try: s = json.loads(sf.read_text()) if sf.exists() else {}
except Exception: s = {}
s["apiKeyHelper"] = "cat " + str(kf)
if model: s["model"] = model
sf.write_text(json.dumps(s, indent=2))
# 2. pre-accept onboarding / trust so the TUI goes straight to a ready prompt.
cj = home / ".claude.json"
try: c = json.loads(cj.read_text()) if cj.exists() else {}
except Exception: c = {}
c["hasCompletedOnboarding"] = True
c.setdefault("theme", "dark")
c["bypassPermissionsModeAccepted"] = True
cj.write_text(json.dumps(c, indent=2))
print(json.dumps({"apiKeyHelper": s["apiKeyHelper"], "model": s.get("model")}))
'''


def lab_agent_setup(
    ssh_alias: str,
    provider: str = "anthropic",
    model: str = "claude-sonnet-4-6",
    api_key: Optional[str] = None,
) -> dict[str, Any]:
    """Prepare a remote instance so a launched claude agent runs AUTONOMOUSLY:
    inject the Anthropic API key (via Claude Code's ``apiKeyHelper``), pre-accept
    onboarding, and pin a valid model. Resolves the key from ``api_key`` → env →
    ``~/.kannaka/config.toml``. NOTE: this uploads the API key to the instance
    (stored 0600 in ~/.claude). Run after lab_ssh_configure, before
    lab_agent_launch."""
    provider = (provider or "anthropic").lower()
    if provider not in ("anthropic", "claude"):
        raise RuntimeError(
            f"lab_agent_setup currently supports the Anthropic (claude) provider, not '{provider}'."
        )
    key = api_key or _resolve_provider_key("anthropic")
    if not key:
        raise RuntimeError(
            "no Anthropic API key found — pass api_key, set ANTHROPIC_API_KEY, or "
            "configure ~/.kannaka/config.toml [llm]."
        )
    remote = _remote_ssh_py(ssh_alias, _AGENT_SETUP_SCRIPT, stdin=json.dumps({"key": key, "model": model}))
    return {
        "ssh_alias": ssh_alias,
        "provider": "anthropic",
        "model": model,
        "configured": True,
        "remote": remote,
        "note": "API key uploaded to the instance (~/.claude, 0600). Now lab_agent_launch, then drive with "
        "lab_agent_send — qBraid's launch --instructions does not auto-submit, so send the task explicitly.",
    }
