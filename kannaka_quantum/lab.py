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

import hashlib
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
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
# Instance leases (T4.1) — extend the per-minute spend doctrine to compute
# --------------------------------------------------------------------------- #
# Per-minute instance/server billing is the same hazard class as the per-minute
# QPUs the circuit bridge refuses outright: a forgotten instance drains the
# budget silently. Every paid compute start records a *lease* (a max wall-time)
# in leases.jsonl; ``lab_reap`` stops anything past its lease; and
# ``lab_agent_launch`` refuses to drive an instance that has no active lease.
# See docs/adr-0001-remote-agent-surface.md.

#: Default lease wall-time (minutes) for a paid compute action.
DEFAULT_LEASE_MINUTES = 60


def _leases_path() -> Path:
    base = os.environ.get("KANNAKA_DATA_DIR") or str(Path.home() / ".kannaka")
    d = Path(base)
    d.mkdir(parents=True, exist_ok=True)
    return d / "leases.jsonl"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None


def _append_lease(record: dict) -> None:
    rec = {"ts": _iso(_now_utc()), **record}
    with open(_leases_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _read_leases() -> dict[str, dict]:
    """Current lease state, folded from the append-only ledger (last write per
    ``instance_id`` wins, merging fields so a later partial record — e.g. a key
    fingerprint or a reap — updates without dropping the rest)."""
    path = _leases_path()
    if not path.exists():
        return {}
    state: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        iid = r.get("instance_id")
        if not iid:
            continue
        state.setdefault(iid, {})
        state[iid].update(r)
    return state


def _record_lease(
    instance_id: str,
    kind: str,
    *,
    profile: Optional[str] = None,
    cluster: Optional[str] = None,
    ssh_alias: Optional[str] = None,
    max_minutes: int = DEFAULT_LEASE_MINUTES,
    event: str = "provision",
) -> dict:
    created = _now_utc()
    minutes = max(1, int(max_minutes))
    rec = {
        "instance_id": instance_id,
        "kind": kind,
        "profile": profile,
        "cluster": cluster,
        "ssh_alias": ssh_alias,
        "created_at": _iso(created),
        "max_minutes": minutes,
        "expires_at": _iso(created + timedelta(minutes=minutes)),
        "status": "active",
        "event": event,
    }
    _append_lease(rec)
    return rec


def _lease_for_alias(ssh_alias: str) -> Optional[dict]:
    for r in _read_leases().values():
        if r.get("ssh_alias") == ssh_alias:
            return r
    return None


def _key_fingerprint(key: str) -> str:
    """A short, non-reversible fingerprint of an API key (never the key itself)."""
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def lab_reap(dry_run: bool = False, now: Optional[datetime] = None) -> dict[str, Any]:
    """Stop every leased instance/server whose lease has expired — the
    cron/systemd-timer-friendly enforcement of :data:`DEFAULT_LEASE_MINUTES`.

    ``dry_run`` reports what would be reaped without stopping anything. Returns
    the reaped and still-active leases so a timer log is self-explanatory."""
    from qbraid_core.services.compute import ComputeClient

    now = now or _now_utc()
    leases = _read_leases()
    expired = [
        r for r in leases.values()
        if r.get("status") == "active" and (_parse_iso(r.get("expires_at", "")) or now) <= now
    ]
    client = None if dry_run else _client(ComputeClient)
    reaped, errors = [], []
    for r in expired:
        iid = r["instance_id"]
        entry = {
            "instance_id": iid,
            "kind": r.get("kind"),
            "expires_at": r.get("expires_at"),
            "ssh_alias": r.get("ssh_alias"),
        }
        if dry_run:
            reaped.append({**entry, "would_stop": True})
            continue
        try:
            if r.get("kind") == "server":
                client.stop_server(cluster_id=r.get("cluster"))
            else:
                client.stop_bma_instance(iid)
            _append_lease({"instance_id": iid, "status": "reaped", "reaped_at": _iso(now), "event": "reap"})
            reaped.append({**entry, "stopped": True})
        except Exception as e:  # pragma: no cover - live-API variance
            errors.append({**entry, "error": f"{type(e).__name__}: {e}"})
    still_active = [
        {"instance_id": r["instance_id"], "expires_at": r.get("expires_at")}
        for r in leases.values()
        if r.get("status") == "active" and r not in expired
    ]
    out = {
        "now": _iso(now),
        "dry_run": dry_run,
        "checked": len(leases),
        "reaped": reaped,
        "reaped_count": len(reaped),
        "still_active": still_active,
    }
    if errors:
        out["errors"] = errors
    return out


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
    max_minutes: int = DEFAULT_LEASE_MINUTES,
) -> dict[str, Any]:
    """Start the Lab server on a compute profile (PAID, per-minute). Records a
    lease so ``lab_reap`` can auto-stop a forgotten server."""
    from qbraid_core.services.compute import ComputeClient

    client = _client(ComputeClient)
    guard = _compute_spend_guard(
        client, allow_spend, max_credits, profile_slug=profile, what=f"Starting the Lab server on '{profile}'"
    )
    res = client.start_server(profile, cluster_id=cluster)
    lease = _record_lease(
        f"server:{cluster or 'default'}", "server", profile=profile, cluster=cluster,
        max_minutes=max_minutes, event="compute_up",
    )
    out = {"started": True, "profile": profile, "spend_guard": guard, "lease": lease, "result": _dump(res)}
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
    max_minutes: int = DEFAULT_LEASE_MINUTES,
) -> dict[str, Any]:
    """Provision (launch) a new on-demand compute instance (PAID, per-minute).

    Records a lease (default 60 min wall-time) so ``lab_reap`` can auto-stop a
    forgotten instance and ``lab_agent_launch`` will target it."""
    from qbraid_core.services.compute import ComputeClient

    client = _client(ComputeClient)
    guard = _compute_spend_guard(
        client, allow_spend, max_credits, profile_slug=profile, what=f"Provisioning an instance on '{profile}'"
    )
    inst = client.provision_bma_instance(profile)
    instance_id = getattr(inst, "instance_id", None)
    lease = None
    if instance_id:
        try:
            alias = ComputeClient.bma_ssh_alias(instance_id)
        except Exception:
            alias = None
        lease = _record_lease(
            instance_id, "instance", profile=profile, ssh_alias=alias, max_minutes=max_minutes, event="provision"
        )
    out = {
        "provisioned": True,
        "profile": profile,
        "instance_id": instance_id,
        "spend_guard": guard,
        "lease": lease,
        "instance": _dump(inst),
        # Surface the post-stop disk floor at PROVISION time (not just at stop):
        # the approving human should see that pausing still bills for disk.
        "stopped_credits_per_min": getattr(inst, "stopped_credits_per_min", None),
        "note": "Even after lab_stop_instance (pause), this instance keeps billing stopped_credits_per_min "
        "for its disk until terminated. Run lab_terminate_instance for the full teardown (frees disk, stops all billing).",
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
    max_minutes: int = DEFAULT_LEASE_MINUTES,
) -> dict[str, Any]:
    """Resume a stopped on-demand instance (PAID, per-minute). Refreshes the
    instance's lease — resuming restarts per-minute billing."""
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
    try:
        alias = ComputeClient.bma_ssh_alias(instance_id)
    except Exception:
        alias = None
    lease = _record_lease(instance_id, "instance", ssh_alias=alias, max_minutes=max_minutes, event="start")
    return {"started": True, "instance_id": instance_id, "spend_guard": guard, "lease": lease, "instance": _dump(inst)}


def lab_stop_instance(instance_id: str) -> dict[str, Any]:
    """Stop (pause) an on-demand instance — disk preserved, running billing
    stops. (A stopped instance still bills ``stopped_credits_per_min`` for disk;
    call :func:`lab_terminate_instance` to free the disk and stop all billing.)"""
    from qbraid_core.services.compute import ComputeClient

    inst = _client(ComputeClient).stop_bma_instance(instance_id)
    return {
        "stopped": True,
        "instance_id": instance_id,
        "instance": _dump(inst),
        "note": "Disk preserved; a stopped instance still bills stopped_credits_per_min until terminated. "
        "Run lab_terminate_instance to free the disk and stop ALL billing.",
    }


def lab_terminate_instance(instance_id: str) -> dict[str, Any]:
    """Terminate an on-demand instance — the FULL teardown: frees the disk and
    stops ALL billing (running *and* ``stopped_credits_per_min``), and destroys
    anything left on it (e.g. an uploaded API key or NATS creds). Unlike
    :func:`lab_stop_instance` (pause, keeps billing disk), this is destructive
    and final. Marks the lease terminated so :func:`lab_reap` won't chase it.

    (Verified live 2026-07-02: ``terminate_bma_instance`` works from the API — no
    qBraid web UI required, contrary to earlier notes.)"""
    from qbraid_core.services.compute import ComputeClient

    inst = _client(ComputeClient).terminate_bma_instance(instance_id)
    _append_lease(
        {"instance_id": instance_id, "status": "terminated", "terminated_at": _iso(_now_utc()), "event": "terminate"}
    )
    return {
        "terminated": True,
        "instance_id": instance_id,
        "instance": _dump(inst),
        "note": "Instance destroyed: disk freed, all billing stopped, and any uploaded secrets are gone.",
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
    allow_unleased: bool = False,
) -> dict[str, Any]:
    """Launch a coding agent (claude / codex / opencode) ON a remote provisioned
    instance over SSH — the kannaka agent driving another agent on cloud compute.
    Requires SSH already configured (lab_ssh_configure) and the instance running.

    GATE (T4.1): refuses an instance with no active lease — an unleased instance
    is one nothing will auto-stop, exactly the runaway-billing hazard leases
    exist to close. Provision via ``lab_provision_instance`` (which records a
    lease), or pass ``allow_unleased=True`` to override deliberately."""
    lease = _lease_for_alias(ssh_alias)
    if not allow_unleased and (lease is None or lease.get("status") != "active"):
        raise RuntimeError(
            f"refusing to launch on '{ssh_alias}': no active lease. Per-minute instances must be "
            "leased so lab_reap can auto-stop them. Provision via lab_provision_instance (records a "
            "lease), or re-run with allow_unleased=True (CLI: --allow-unleased) to override."
        )
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
    i_know: bool = False,
) -> dict[str, Any]:
    """Prepare a remote instance so a launched claude agent runs AUTONOMOUSLY:
    inject the Anthropic API key (via Claude Code's ``apiKeyHelper``), pre-accept
    onboarding, and pin a valid model. Resolves the key from ``api_key`` → env →
    ``~/.kannaka/config.toml``. NOTE: this uploads the API key to the instance
    (stored 0600 in ~/.claude). Run after lab_ssh_configure, before
    lab_agent_launch.

    Blast-radius guard (T4.2): the uploaded key on a bypass-permissions remote
    agent is the fleet's largest blast radius (ADR-0001). This refuses a key
    identical to your primary ``ANTHROPIC_API_KEY`` unless ``i_know=True`` —
    upload a scoped, per-instance key instead. The key's fingerprint (never the
    key) is recorded against the instance's lease, and ``lab_agent_teardown``
    removes it when done."""
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
    primary = os.environ.get("ANTHROPIC_API_KEY")
    if primary and key.strip() == primary.strip() and not i_know:
        raise RuntimeError(
            "refusing to upload your PRIMARY ANTHROPIC_API_KEY to a remote instance — a "
            "prompt-injected or compromised bypass-permissions agent there would hold your full-access "
            "key (ADR-0001's largest blast radius). Mint a scoped, per-instance key (Admin API "
            "workspace key — see docs/adr-0001-remote-agent-surface.md) and pass it as api_key, or "
            "re-run with i_know=True (CLI: --i-know) to override deliberately."
        )
    remote = _remote_ssh_py(ssh_alias, _AGENT_SETUP_SCRIPT, stdin=json.dumps({"key": key, "model": model}))
    fingerprint = _key_fingerprint(key)
    lease = _lease_for_alias(ssh_alias)
    lease_id = lease["instance_id"] if lease else ssh_alias
    _append_lease(
        {"instance_id": lease_id, "ssh_alias": ssh_alias, "key_fingerprint": fingerprint,
         "key_set_at": _iso(_now_utc()), "event": "key_setup"}
    )
    return {
        "ssh_alias": ssh_alias,
        "provider": "anthropic",
        "model": model,
        "configured": True,
        "key_fingerprint": fingerprint,
        "remote": remote,
        "note": "Scoped API key uploaded to the instance (~/.claude, 0600); fingerprint recorded in the "
        "lease. Now lab_agent_launch, then drive with lab_agent_send. Run lab_agent_teardown when done "
        "to delete the remote key.",
    }


#: Remote teardown script — remove the uploaded key file + scrub the apiKeyHelper
#: reference so a torn-down instance can't keep authenticating. Prints what it removed.
_AGENT_TEARDOWN_SCRIPT = r'''
import json, pathlib
home = pathlib.Path.home()
removed = []
kf = home / ".claude" / "anthropic_key"
if kf.exists():
    kf.unlink(); removed.append(str(kf))
sf = home / ".claude" / "settings.json"
try:
    s = json.loads(sf.read_text())
    if "apiKeyHelper" in s:
        del s["apiKeyHelper"]; sf.write_text(json.dumps(s, indent=2)); removed.append("apiKeyHelper")
except Exception:
    pass
print(json.dumps({"removed": removed}))
'''


def lab_agent_teardown(ssh_alias: str) -> dict[str, Any]:
    """Delete the uploaded Anthropic key from a remote instance and print a
    rotation reminder (T4.2). Run when done with a remote agent — the key lived
    on third-party compute, so rotating it after teardown is the safe default."""
    remote = _remote_ssh_py(ssh_alias, _AGENT_TEARDOWN_SCRIPT)
    lease = _lease_for_alias(ssh_alias)
    fingerprint = lease.get("key_fingerprint") if lease else None
    if lease:
        _append_lease(
            {"instance_id": lease["instance_id"], "ssh_alias": ssh_alias,
             "key_fingerprint": None, "key_torn_down_at": _iso(_now_utc()), "event": "key_teardown"}
        )
    return {
        "ssh_alias": ssh_alias,
        "torn_down": True,
        "removed_key_fingerprint": fingerprint,
        "remote": remote,
        "rotation_reminder": (
            "ROTATE this key now: it resided on a third-party instance. If it was a scoped Admin-API "
            "workspace key, revoke it in the Anthropic console; if it was a broader key, rotate it."
        ),
    }


# --------------------------------------------------------------------------- #
# Phase 5 — remote shell + QuantumOS boot
# --------------------------------------------------------------------------- #
#: Cap on captured remote stdout/stderr. The Rust bridge drains pipes only
#: after the child exits, so every subcommand must print one *small* JSON
#: object — unbounded remote output would risk a pipe-buffer deadlock there.
EXEC_OUTPUT_CAP = 8000


def _remote_ssh_sh(ssh_alias: str, command: str, timeout: int = 90, stdin: str = "") -> subprocess.CompletedProcess:
    """Run a shell command on the remote instance over SSH.

    Same transport discipline as :func:`_remote_ssh_py` (BatchMode, pinned
    known_hosts via HostKeyAlias), but the payload is ``bash -lc <command>``
    shell-quoted as one argument so the remote shell can't re-split it.
    Returns the raw CompletedProcess — callers decide whether a non-zero rc
    is an error (a failing command is often a *result*, not a failure)."""
    remote = "bash -lc " + shlex.quote(command)
    return subprocess.run(
        [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={_known_hosts_path()}",
            "-o", f"HostKeyAlias={ssh_alias}",
            ssh_alias,
            remote,
        ],
        input=stdin, capture_output=True, text=True, timeout=timeout,
    )


def _cap(text: str) -> tuple[str, bool]:
    """Trim to the LAST ``EXEC_OUTPUT_CAP`` chars (the end of a build/boot log
    is where the verdict lives)."""
    t = text or ""
    if len(t) <= EXEC_OUTPUT_CAP:
        return t, False
    return t[-EXEC_OUTPUT_CAP:], True


def lab_exec(ssh_alias: str, command: str, timeout_secs: int = 90) -> dict[str, Any]:
    """Run a shell command on a provisioned instance over its SSH alias and
    return rc + capped output. Unlike most lab functions a non-zero exit is
    returned as data (``ok: false``), not raised — the caller usually wants
    the diagnostics. Costs nothing beyond the instance's own per-minute bill.

    No lease gate: this is an interactive, per-call-approved primitive (the
    agent harness asks before every un-allowlisted call). Long-lived workloads
    belong in :func:`lab_qos_boot` / :func:`lab_agent_launch`, which do gate."""
    if not command.strip():
        raise RuntimeError("lab_exec: empty command")
    r = _remote_ssh_sh(ssh_alias, command, timeout=timeout_secs)
    out, out_trunc = _cap(r.stdout)
    err, err_trunc = _cap(r.stderr)
    return {
        "ssh_alias": ssh_alias,
        "command": command,
        "rc": r.returncode,
        "ok": r.returncode == 0,
        "stdout": out,
        "stderr": err,
        "truncated": out_trunc or err_trunc,
    }


#: Default QuantumOS clone source for lab_qos_boot.
QOS_DEFAULT_REPO = "https://github.com/flaukowski/QuantumOS.git"

#: Idempotent remote prep: install missing deps, clone-or-update the repo,
#: build the kernel. Tokens (@REPO@ etc.) are substituted with `.replace`, not
#: `.format`, because the rootless block needs literal shell ``${...}``.
#: Kept quiet — only stage markers print, so the bridge's small-JSON contract
#: holds. Dep strategy, learned live on a real qBraid instance (2026-07-02):
#: qBraid's image runs as jovyan with NO passwordless sudo but ships
#: gcc/make/git/tmux — only QEMU is missing, and conda-forge has no
#: qemu-system for linux-64. So: PATH-visible qemu wins; else sudo apt; else
#: a fully ROOTLESS install — apt update/download into $HOME state dirs
#: (no root needed), dpkg -x into ~/.local/qemu-root, a wrapper that sets
#: LD_LIBRARY_PATH (debs split across usr/lib and lib) + -L firmware dirs,
#: then an ldd loop that downloads the soname closure (libpmem pulls
#: libndctl/libdaxctl/libkmod transitively; t64-suffixed names on noble).
_QOS_PREP_SCRIPT = """
set -e
export DEBIAN_FRONTEND=noninteractive
export PATH="$HOME/.local/bin:$PATH"
need=""
command -v qemu-system-x86_64 >/dev/null 2>&1 || need="$need qemu"
command -v gcc  >/dev/null 2>&1 || need="$need build-essential"
command -v tmux >/dev/null 2>&1 || need="$need tmux"
command -v git  >/dev/null 2>&1 || need="$need git"
if [ -n "$need" ]; then
  if sudo -n true 2>/dev/null; then
    echo "[deps] apt installing:$need"
    pkgs=$(echo "$need" | sed 's/qemu/qemu-system-x86/')
    sudo apt-get update -qq >/dev/null
    sudo apt-get install -y -qq $pkgs >/dev/null
  elif [ "$(echo $need | tr -d ' ')" = "qemu" ]; then
    echo "[deps] no sudo - rootless QEMU install (apt download + dpkg -x)"
    R="$HOME/.local/qemu-root"; A="$HOME/apt/cache/archives"
    mkdir -p "$R" "$HOME/.local/bin" "$HOME/apt/lists/partial" "$A/partial"
    APTO="-o Dir::State::Lists=$HOME/apt/lists -o Dir::Cache=$HOME/apt/cache -o Debug::NoLocking=1"
    apt-get update $APTO -qq >/dev/null 2>&1 || true
    cd "$A"
    apt-get download $APTO qemu-system-x86 qemu-system-common qemu-system-data seabios libfdt1 libslirp0 libpixman-1-0 >/dev/null
    for d in "$A"/*.deb; do dpkg -x "$d" "$R"; done
    printf '%s\\n' '#!/bin/sh' 'R=$HOME/.local/qemu-root' \\
      'export LD_LIBRARY_PATH="$R/usr/lib/x86_64-linux-gnu:$R/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH"' \\
      'export QEMU_MODULE_DIR="$R/usr/lib/x86_64-linux-gnu/qemu"' \\
      'exec "$R/usr/bin/qemu-system-x86_64" -L "$R/usr/share/qemu" -L "$R/usr/share/seabios" "$@"' \\
      > "$HOME/.local/bin/qemu-system-x86_64"
    chmod +x "$HOME/.local/bin/qemu-system-x86_64"
    for i in 1 2 3 4 5 6; do
      missing=$(LD_LIBRARY_PATH="$R/usr/lib/x86_64-linux-gnu:$R/lib/x86_64-linux-gnu" ldd "$R/usr/bin/qemu-system-x86_64" 2>/dev/null | grep 'not found' | awk '{print $1}' | sort -u)
      [ -z "$missing" ] && break
      for lib in $missing; do
        base=$(echo "$lib" | sed 's/\\.so\\..*//; s/_/-/g'); ver=$(echo "$lib" | sed 's/.*\\.so\\.//' | cut -d. -f1)
        for cand in "${base}${ver}" "${base}${ver}t64" "${base}-${ver}" "$base"; do
          apt-get download $APTO "$cand" >/dev/null 2>&1 && break
        done
      done
      for d in "$A"/*.deb; do dpkg -x "$d" "$R"; done
    done
    qemu-system-x86_64 --version >/dev/null
    echo "[deps] rootless QEMU ready"
  else
    echo "[deps] MISSING:$need and no passwordless sudo on this host" >&2
    exit 41
  fi
fi
if [ -d "$HOME/QuantumOS/.git" ]; then
  echo "[repo] updating existing clone"
  git -C "$HOME/QuantumOS" fetch --quiet origin
  @REF_CHECKOUT@
else
  echo "[repo] cloning @REPO@"
  git clone --quiet @REPO@ "$HOME/QuantumOS"
  @REF_CHECKOUT_FRESH@
fi
echo "[build] make kernel"
make -C "$HOME/QuantumOS" kernel >/dev/null
ls "$HOME"/QuantumOS/build/*/kernel.elf32 >/dev/null
echo "[build] ok"
"""

#: Boot inside a detached tmux session so the QEMU serial console outlives
#: this call and any watcher window. ``exec bash`` keeps the pane alive after
#: QEMU exits so a crash's last output stays readable. ``-nic none -vga none``
#: keeps the rootless install's firmware needs down to seabios + the
#: multiboot/linuxboot option ROMs.
_QOS_BOOT_SCRIPT = """
set -e
export PATH="$HOME/.local/bin:$PATH"
kernel=$(ls "$HOME"/QuantumOS/build/*/kernel.elf32 | head -1)
tmux new-session -d -s @SESSION@ \
  "export PATH=\\"$HOME/.local/bin:$PATH\\"; qemu-system-x86_64 -kernel $kernel @APPEND@ -serial stdio -m 128M -display none -vga none -nic none -no-reboot; echo; echo '[qemu exited]'; exec bash"
sleep @SETTLE@
tmux capture-pane -p -S -200 -t @SESSION@ | grep -v '^$' | tail -n 60
"""


#: noVNC release the graphical watch pins (proven live 2026-07-02).
QOS_NOVNC_VERSION = "1.5.0"
#: Default local/remote web port for the websockify→VNC bridge.
QOS_DEFAULT_WEB_PORT = 6080

#: Rootless install of the graphical-watch deps: websockify (pip --user, no sudo)
#: + a pinned noVNC tarball into ~/.local. Idempotent — skips what's present.
_QOS_NOVNC_INSTALL = """
set -e
export PATH="$HOME/.local/bin:$PATH"
if ! command -v websockify >/dev/null 2>&1; then
  echo "[novnc] pip install --user websockify"
  python3 -m pip install --user --quiet websockify
fi
NOVNC="$HOME/.local/noVNC-@VER@"
if [ ! -f "$NOVNC/vnc_lite.html" ]; then
  echo "[novnc] fetching noVNC @VER@"
  curl -fsSL https://github.com/novnc/noVNC/archive/refs/tags/v@VER@.tar.gz | tar xz -C "$HOME/.local"
fi
[ -f "$NOVNC/vnc_lite.html" ] || { echo "[novnc] MISSING vnc_lite.html after fetch" >&2; exit 42; }
echo "[novnc] ready: $NOVNC"
"""

#: Graphical boot: QEMU with a real VGA framebuffer over VNC, started PAUSED
#: (``-S``) with a QEMU monitor unix socket so lab_watch resumes it (``cont``)
#: once the operator's browser is attached. websockify serves noVNC + proxies
#: the web port to the VNC port, both in their own detached tmux sessions so
#: they outlive this call. Serial goes to a file (VNC owns the display) so
#: boot-readiness is still greppable.
_QOS_BOOT_GRAPHICAL_SCRIPT = """
set -e
export PATH="$HOME/.local/bin:$PATH"
kernel=$(ls "$HOME"/QuantumOS/build/*/kernel.elf32 | head -1)
mon="$HOME/.qos-@SESSION@.mon"; ser="$HOME/.qos-@SESSION@.serial"
rm -f "$mon" "$ser"
tmux new-session -d -s @SESSION@ \
  "export PATH=\\"$HOME/.local/bin:$PATH\\"; qemu-system-x86_64 -kernel $kernel @APPEND@ -m 128M -vga std -vnc 127.0.0.1:@VNCDISP@ -S -monitor unix:$mon,server,nowait -serial file:$ser -no-reboot; echo; echo '[qemu exited]'; exec bash"
tmux new-session -d -s @SESSION@-web \
  "export PATH=\\"$HOME/.local/bin:$PATH\\"; websockify --web $HOME/.local/noVNC-@NOVNCVER@ @WEBPORT@ 127.0.0.1:@VNCPORT@; exec bash"
sleep @SETTLE@
echo "[graphical] qemu PAUSED (-S) + VGA over VNC :@VNCPORT@; websockify :@WEBPORT@; monitor=$mon"
"""


def _qos_booted(lines: list[str]) -> bool:
    """A kernel that printed its ready line — or is actively ticking — is up.
    The ready line can scroll out of even a deep capture once services and
    user processes start logging, so the tick heartbeat counts too."""
    return any("QuantumOS ready" in ln or "Timer tick" in ln for ln in lines)


def lab_qos_boot(
    ssh_alias: str,
    repo: str = QOS_DEFAULT_REPO,
    ref: Optional[str] = None,
    session: str = "qos",
    fresh: bool = False,
    allow_unleased: bool = False,
    timeout_secs: int = 540,
    qseed: Optional[str] = None,
    graphical: bool = False,
    web_port: int = QOS_DEFAULT_WEB_PORT,
) -> dict[str, Any]:
    """Boot QuantumOS in QEMU on a provisioned instance, inside a detached
    tmux session, and return the serial-console tail (the kernel prints
    "QuantumOS ready" when it reaches the idle loop, then timer ticks).

    ``graphical=True`` boots with a real VGA framebuffer over VNC instead of the
    text serial console: QEMU comes up PAUSED (``-S``) with ``-vga std -vnc`` and
    a monitor socket, and websockify + noVNC (v1.5.0) are installed rootlessly to
    serve it. The return carries the VNC/websockify/monitor coordinates; run
    :func:`lab_watch` (CLI ``lab-watch``) to open an SSH ``-L`` tunnel, launch the
    browser at ``vnc_lite.html``, and resume the VM (``cont``). Text mode stays
    the default — the display contract is unchanged unless you opt in.

    ``qseed`` hands the kernel boot entropy on its Multiboot command line
    (``qseed=<hex>``, QuantumOS PR #40): pass ``"reservoir"`` to draw 64 RAW
    bits from the local quantum-entropy reservoir (real QPU bits with a
    provenance chain; raises if the reservoir is empty — never a silent PRNG
    fallback), or an explicit hex value (≤16 digits). The kernel echoes the
    accepted seed zero-padded on serial; ``qseed_confirmed`` reports whether
    that exact echo was observed in the boot tail (fresh boots only —
    honest stamping, no claim without the observation).

    Idempotent: deps are installed only if missing, the clone is updated in
    place, and an already-running session is reported (with its current tail)
    rather than clobbered — pass ``fresh=True`` to kill it and reboot from a
    rebuilt kernel. Watch live with lab_watch, or manually:
    ``ssh -t <alias> tmux attach -t <session>``.

    GATE: same active-lease requirement as lab_agent_launch — this starts a
    long-lived workload on per-minute compute, exactly what leases exist to
    bound. Override deliberately with ``allow_unleased=True``."""
    if not session.replace("-", "").replace("_", "").isalnum():
        raise RuntimeError(f"lab_qos_boot: invalid tmux session name '{session}'")
    qseed_hex: Optional[str] = None
    qseed_provenance = None
    if qseed:
        if qseed == "reservoir":
            from . import entropy

            d = entropy.draw(64, expand=False)
            qseed_hex = f"{d['int']:016x}"
            qseed_provenance = d["provenance"]
        else:
            s = qseed.lower().removeprefix("0x")
            if not (1 <= len(s) <= 16) or any(c not in "0123456789abcdef" for c in s):
                raise RuntimeError(
                    f"lab_qos_boot: qseed must be 'reservoir' or up to 16 hex digits, got '{qseed}'"
                )
            qseed_hex = s
    lease = _lease_for_alias(ssh_alias)
    if not allow_unleased and (lease is None or lease.get("status") != "active"):
        raise RuntimeError(
            f"refusing to boot on '{ssh_alias}': no active lease. Per-minute instances must be "
            "leased so lab_reap can auto-stop them. Provision via lab_provision_instance (records a "
            "lease), or re-run with allow_unleased=True (CLI: --allow-unleased) to override."
        )
    attach = f"ssh -t {ssh_alias} tmux attach -t {session}"

    # An existing session either satisfies the request (report it) or, with
    # fresh=True, gets killed before the rebuild.
    has = _remote_ssh_sh(ssh_alias, f"tmux has-session -t {shlex.quote(session)} 2>/dev/null", timeout=30)
    if has.returncode == 0:
        if not fresh:
            tail = _remote_ssh_sh(
                ssh_alias,
                f"tmux capture-pane -p -S -200 -t {shlex.quote(session)} | grep -v '^$' | tail -n 40",
                timeout=30,
            )
            lines = [ln for ln in (tail.stdout or "").splitlines() if ln.strip()]
            return {
                "ssh_alias": ssh_alias,
                "session": session,
                "already_running": True,
                "booted": _qos_booted(lines),
                "tail": lines,
                "attach": attach,
                "note": "session already exists — pass fresh=true to rebuild and reboot",
            }
        _remote_ssh_sh(ssh_alias, f"tmux kill-session -t {shlex.quote(session)}", timeout=30)

    ref_q = shlex.quote(ref) if ref else ""
    prep = (
        _QOS_PREP_SCRIPT.replace("@REPO@", shlex.quote(repo))
        .replace(
            "@REF_CHECKOUT@",
            (
                # checkout failure must abort (set -e); the ff-merge to the remote
                # ref is best-effort (sha refs have no origin/<sha> to merge).
                f'git -C "$HOME/QuantumOS" checkout --quiet {ref_q} && '
                f'(git -C "$HOME/QuantumOS" merge --ff-only --quiet origin/{ref_q} 2>/dev/null || true)'
                if ref
                else 'git -C "$HOME/QuantumOS" pull --ff-only --quiet'
            ),
        )
        .replace(
            "@REF_CHECKOUT_FRESH@",
            f'git -C "$HOME/QuantumOS" checkout --quiet {ref_q}' if ref else "true",
        )
    )
    r = _remote_ssh_sh(ssh_alias, prep, timeout=timeout_secs)
    if r.returncode != 0:
        err, _ = _cap(r.stderr)
        out, _ = _cap(r.stdout)
        raise RuntimeError(f"QuantumOS prep/build failed (rc={r.returncode}): {err or out}"[:1200])

    if graphical:
        vnc_display = 0
        vnc_port = 5900 + vnc_display
        nv = _remote_ssh_sh(ssh_alias, _QOS_NOVNC_INSTALL.replace("@VER@", QOS_NOVNC_VERSION), timeout=timeout_secs)
        if nv.returncode != 0:
            nverr, _ = _cap(nv.stderr)
            nvout, _ = _cap(nv.stdout)
            raise RuntimeError(f"noVNC/websockify install failed (rc={nv.returncode}): {nverr or nvout}"[:1200])
        gboot = (
            _QOS_BOOT_GRAPHICAL_SCRIPT.replace("@SESSION@", session)
            .replace("@SETTLE@", "3")
            .replace("@APPEND@", f"-append qseed={qseed_hex}" if qseed_hex else "")
            .replace("@VNCDISP@", str(vnc_display))
            .replace("@VNCPORT@", str(vnc_port))
            .replace("@WEBPORT@", str(int(web_port)))
            .replace("@NOVNCVER@", QOS_NOVNC_VERSION)
        )
        gb = _remote_ssh_sh(ssh_alias, gboot, timeout=60)
        if gb.returncode != 0:
            gerr, _ = _cap(gb.stderr)
            raise RuntimeError(f"QuantumOS graphical boot launch failed (rc={gb.returncode}): {gerr}"[:1200])
        gout: dict[str, Any] = {
            "ssh_alias": ssh_alias,
            "session": session,
            "already_running": False,
            "graphical": True,
            "paused": True,  # started with -S; lab_watch resumes it (cont)
            "booted": False,
            "vnc_port": vnc_port,
            "web_port": int(web_port),
            "monitor_socket": f"~/.qos-{session}.mon",
            "tail": [ln for ln in (gb.stdout or "").splitlines() if ln.strip()],
            "watch": f"kannaka-quantum lab-watch --ssh-alias {ssh_alias} --session {session} --web-port {int(web_port)}",
            "note": "QEMU booted PAUSED with VGA over VNC; run lab_watch (CLI: lab-watch) to open an SSH -L "
            "tunnel + the browser at vnc_lite.html and resume the VM (cont).",
        }
        if qseed_hex:
            gout["qseed"] = qseed_hex
            if qseed_provenance is not None:
                gout["qseed_provenance"] = qseed_provenance
        return gout

    boot = (
        _QOS_BOOT_SCRIPT.replace("@SESSION@", session)
        .replace("@SETTLE@", "4")
        .replace("@APPEND@", f"-append qseed={qseed_hex}" if qseed_hex else "")
    )
    b = _remote_ssh_sh(ssh_alias, boot, timeout=60)
    if b.returncode != 0:
        err, _ = _cap(b.stderr)
        raise RuntimeError(f"QuantumOS boot launch failed (rc={b.returncode}): {err}"[:1200])
    lines = [ln for ln in (b.stdout or "").splitlines() if ln.strip()]
    booted = _qos_booted(lines)
    out: dict[str, Any] = {
        "ssh_alias": ssh_alias,
        "session": session,
        "already_running": False,
        "booted": booted,
        "tail": lines,
        "attach": attach,
        "note": (
            "QuantumOS is up — attach with lab_watch or the attach command"
            if booted
            else "boot launched but 'QuantumOS ready' not seen yet — re-check with "
            f"lab_exec 'tmux capture-pane -p -t {session} | tail -n 25'"
        ),
    }
    if qseed_hex:
        # The kernel re-prints the accepted seed zero-padded uppercase
        # (early_console_write_hex) in the FIRST boot lines — by capture time
        # the kernel has logged well past the returned tail, so confirm with
        # a dedicated grep over the full scrollback rather than the tail.
        echo = f"{int(qseed_hex, 16):016X}"
        # grep -q exits 0 only on a real match (ssh/tmux failure is nonzero
        # too, which honestly reads as unconfirmed).
        g = _remote_ssh_sh(
            ssh_alias,
            f"tmux capture-pane -p -S -300 -t {shlex.quote(session)} | grep -q {echo}",
            timeout=30,
        )
        out["qseed"] = qseed_hex
        out["qseed_confirmed"] = g.returncode == 0
        if qseed_provenance is not None:
            out["qseed_provenance"] = qseed_provenance
    return out


def _qos_cont_command(session: str) -> str:
    """Remote shell that sends ``cont`` to the QEMU monitor unix socket to
    un-pause a graphical (``-S``) QuantumOS VM. Passes the socket path as argv so
    there's no quoting hazard, and reads the monitor banner before writing."""
    return (
        f'mon="$HOME/.qos-{session}.mon"; python3 - "$mon" <<\'PY\'\n'
        "import socket, sys\n"
        "s = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])\n"
        "try: s.recv(4096)\n"
        "except Exception: pass\n"
        's.sendall(b"cont\\n"); print("cont sent")\n'
        "PY\n"
    )


def lab_watch(
    ssh_alias: str,
    session: str = "qos",
    web_port: int = QOS_DEFAULT_WEB_PORT,
    local_port: Optional[int] = None,
    resume: bool = True,
    open_browser: bool = True,
) -> dict[str, Any]:
    """Open the graphical QuantumOS watch for a ``lab_qos_boot(graphical=True)``
    VM: an SSH ``-L`` tunnel (local → the remote websockify), the browser at
    noVNC's ``vnc_lite.html``, and (``resume=True``) a monitor ``cont`` to
    un-pause the ``-S`` VM.

    Runs LOCALLY on the operator's machine — it launches a *detached* SSH tunnel
    process (kill its PID to stop watching) and opens the default browser.
    Returns the URL + tunnel PID. Text-mode /qos doesn't need this (attach the
    tmux serial console instead)."""
    local_port = int(local_port or web_port)
    resumed: Optional[bool] = None
    if resume:
        r = _remote_ssh_sh(ssh_alias, _qos_cont_command(session), timeout=30)
        resumed = r.returncode == 0

    tunnel = subprocess.Popen(
        [
            "ssh", "-N",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={_known_hosts_path()}",
            "-o", f"HostKeyAlias={ssh_alias}",
            "-L", f"{local_port}:127.0.0.1:{int(web_port)}",
            ssh_alias,
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://localhost:{local_port}/vnc_lite.html?autoconnect=1&resize=scale"
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    return {
        "ssh_alias": ssh_alias,
        "session": session,
        "url": url,
        "local_port": local_port,
        "web_port": int(web_port),
        "tunnel_pid": tunnel.pid,
        "resumed": resumed,
        "note": f"SSH -L tunnel open (pid {tunnel.pid}) + browser launched at vnc_lite.html; VM resumed (cont). "
        f"Stop watching by killing pid {tunnel.pid}.",
    }
