"""JSON command-line bridge — the surface the Kannaka coding agent shells out
to. Every subcommand prints a single JSON object to stdout (errors included),
so a caller can parse it directly.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from . import core
from . import lab


def _parse_json_arg(s: Optional[str]):
    """Parse a CLI arg that the Rust bridge passes as JSON (dict/list), or as a
    comma-separated list. Returns None for empty/missing."""
    if not s:
        return None
    s = s.strip()
    if s.startswith("[") or s.startswith("{"):
        return json.loads(s)
    return [x.strip() for x in s.split(",") if x.strip()]


def _parse_floats(s: str) -> list[float]:
    s = s.strip()
    if s.startswith("["):
        return [float(x) for x in json.loads(s)]
    return [float(x) for x in s.split(",") if x.strip()]


def _parse_strs(s: Optional[str]) -> Optional[list[str]]:
    if not s:
        return None
    s = s.strip()
    if s.startswith("["):
        return [str(x) for x in json.loads(s)]
    return [x.strip() for x in s.split(",") if x.strip()]


_DEVICE_HELP = (
    "device id (default: free qBraid simulator). Use 'openquantum:<backend>' "
    "(e.g. openquantum:iqm:garnet) for a real QPU — spends Spark credits."
)


def _add_spend_opts(p: argparse.ArgumentParser) -> None:
    """Provider-routing spend guard shared by run/qrng/recall.

    No-ops on the free qBraid simulator; required to run on OpenQuantum, which
    has no free tier (1 credit = $2; default ceiling 1 credit).
    """
    p.add_argument("--allow-spend", action="store_true", help="permit a credit-spending OpenQuantum run")
    p.add_argument("--max-credits", type=float, default=None, help="credit ceiling for an OpenQuantum run (default 1.0)")
    p.add_argument("--subcategory", default=None, help="OpenQuantum job_subcategory_id workload tag")


def _add_lab_spend_opts(p: argparse.ArgumentParser) -> None:
    """Spend guard for paid qBraid Lab compute (per-minute billing)."""
    p.add_argument("--allow-spend", action="store_true", help="permit a credit-spending compute action")
    p.add_argument("--max-credits", type=float, default=None, help="committed credit ceiling (default 60)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kannaka-quantum",
        description="Kannaka quantum bridge — circuits, QRNG, and resonance recall on qBraid "
        "(free simulator) or OpenQuantum (real QPUs, spends Spark credits).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("devices", help="list quantum devices (qBraid + OpenQuantum)")
    d.add_argument("--online", action="store_true", help="only ONLINE devices")

    r = sub.add_parser("run", help="run an OpenQASM 3 circuit")
    r.add_argument("--qasm", help='OpenQASM 3 source ("-" or omit reads stdin)')
    r.add_argument("--qasm-file", help="path to an OpenQASM 3 file")
    r.add_argument("--device", default=core.DEFAULT_DEVICE, help=_DEVICE_HELP)
    r.add_argument("--shots", type=int, default=100)
    _add_spend_opts(r)

    g = sub.add_parser("qrng", help="quantum random bits")
    g.add_argument("--bits", type=int, default=8)
    g.add_argument("--device", default=core.DEFAULT_DEVICE, help=_DEVICE_HELP)
    _add_spend_opts(g)

    rc = sub.add_parser("recall", help="resonance recall as amplitude amplification")
    rc.add_argument("--amplitudes", required=True, help="comma-separated or JSON list of resonances")
    rc.add_argument("--labels", help="comma-separated or JSON list of labels")
    rc.add_argument("--shots", type=int, default=1024)
    rc.add_argument("--no-amplify", action="store_true", help="skip amplitude amplification")
    rc.add_argument("--device", default=core.DEFAULT_DEVICE, help=_DEVICE_HELP)
    _add_spend_opts(rc)

    # --- qBraid Lab / infrastructure ---------------------------------------
    sub.add_parser("lab-credits", help="show qBraid credit balance")

    le = sub.add_parser("lab-list-envs", help="list qBraid environments")
    le.add_argument("--page", type=int, default=1)
    le.add_argument("--limit", type=int, default=20)

    ei = sub.add_parser("lab-env-info", help="get one environment's metadata")
    ei.add_argument("--slug", required=True)

    lp = sub.add_parser("lab-list-profiles", help="list compute profiles + per-minute cost")
    lp.add_argument("--gpu-only", action="store_true")
    lp.add_argument("--available-only", action="store_true")
    lp.add_argument("--plan", default=None)
    lp.add_argument("--limit", type=int, default=None)

    cs = sub.add_parser("lab-compute-status", help="Lab server status")
    cs.add_argument("--cluster", default=None)

    cu = sub.add_parser("lab-compute-usage", help="compute usage + credit rates")
    cu.add_argument("--days", type=int, default=None)

    sub.add_parser("lab-list-instances", help="list on-demand compute instances")
    sub.add_parser("lab-list-kernels", help="list local Jupyter kernels")

    ce = sub.add_parser("lab-create-env", help="create a qBraid environment")
    ce.add_argument("--name", required=True)
    ce.add_argument("--description", default=None)
    ce.add_argument("--python-version", default=None)
    ce.add_argument("--packages", default=None, help="JSON dict {name:ver} or list/CSV of names")
    ce.add_argument("--kernel-name", default=None)
    ce.add_argument("--visibility", default="private")
    ce.add_argument("--tags", default=None, help="JSON list or CSV")

    de = sub.add_parser("lab-delete-env", help="delete a qBraid environment")
    de.add_argument("--slug", required=True)

    pi = sub.add_parser("lab-pip-install", help="pip install into a local env (in-Lab only)")
    pi.add_argument("--env-id", required=True)
    pi.add_argument("--packages", required=True, help="JSON list or CSV of package specs")
    pi.add_argument("--upgrade-pip", action="store_true")

    pf = sub.add_parser("lab-pip-freeze", help="list packages in a local env (in-Lab only)")
    pf.add_argument("--env-id", required=True)

    ak = sub.add_parser("lab-add-kernel", help="register a Jupyter kernel (in-Lab only)")
    ak.add_argument("--environment", required=True)
    rk = sub.add_parser("lab-remove-kernel", help="remove a Jupyter kernel (in-Lab only)")
    rk.add_argument("--environment", required=True)

    up = sub.add_parser("lab-compute-up", help="start the Lab server on a profile (PAID)")
    up.add_argument("--profile", required=True)
    up.add_argument("--cluster", default=None)
    up.add_argument("--wait", action="store_true")
    up.add_argument("--timeout", type=float, default=None)
    _add_lab_spend_opts(up)

    dn = sub.add_parser("lab-compute-down", help="stop the Lab server (preserves disk)")
    dn.add_argument("--cluster", default=None)

    pv = sub.add_parser("lab-provision-instance", help="provision a new on-demand instance (PAID)")
    pv.add_argument("--profile", required=True)
    pv.add_argument("--wait", action="store_true")
    pv.add_argument("--timeout", type=float, default=None)
    _add_lab_spend_opts(pv)

    si = sub.add_parser("lab-start-instance", help="resume a stopped instance (PAID)")
    si.add_argument("--instance-id", required=True)
    _add_lab_spend_opts(si)

    sp = sub.add_parser("lab-stop-instance", help="stop (pause) an instance (preserves disk)")
    sp.add_argument("--instance-id", required=True)

    sub.add_parser("mcp", help="launch the MCP server (stdio)")
    return p


def _dispatch_lab(args) -> Optional[dict]:
    """Run a ``lab-*`` subcommand; returns its result dict, or None if ``cmd``
    is not a lab command."""
    cmd = args.cmd
    if cmd == "lab-credits":
        return lab.lab_credits()
    if cmd == "lab-list-envs":
        return lab.lab_list_envs(page=args.page, limit=args.limit)
    if cmd == "lab-env-info":
        return lab.lab_env_info(args.slug)
    if cmd == "lab-list-profiles":
        return lab.lab_list_profiles(
            gpu_only=args.gpu_only or None,
            available_only=args.available_only,
            plan=args.plan,
            limit=args.limit,
        )
    if cmd == "lab-compute-status":
        return lab.lab_compute_status(cluster=args.cluster)
    if cmd == "lab-compute-usage":
        return lab.lab_compute_usage(days=args.days)
    if cmd == "lab-list-instances":
        return lab.lab_list_instances()
    if cmd == "lab-list-kernels":
        return lab.lab_list_kernels()
    if cmd == "lab-create-env":
        return lab.lab_create_env(
            args.name,
            description=args.description,
            python_version=args.python_version,
            packages=_parse_json_arg(args.packages),
            kernel_name=args.kernel_name,
            visibility=args.visibility,
            tags=_parse_json_arg(args.tags),
        )
    if cmd == "lab-delete-env":
        return lab.lab_delete_env(args.slug)
    if cmd == "lab-pip-install":
        return lab.lab_pip_install(args.env_id, _parse_json_arg(args.packages) or [], upgrade_pip=args.upgrade_pip)
    if cmd == "lab-pip-freeze":
        return lab.lab_pip_freeze(args.env_id)
    if cmd == "lab-add-kernel":
        return lab.lab_add_kernel(args.environment)
    if cmd == "lab-remove-kernel":
        return lab.lab_remove_kernel(args.environment)
    if cmd == "lab-compute-up":
        return lab.lab_compute_up(
            args.profile, allow_spend=args.allow_spend, max_credits=args.max_credits,
            cluster=args.cluster, wait=args.wait, timeout=args.timeout,
        )
    if cmd == "lab-compute-down":
        return lab.lab_compute_down(cluster=args.cluster)
    if cmd == "lab-provision-instance":
        return lab.lab_provision_instance(
            args.profile, allow_spend=args.allow_spend, max_credits=args.max_credits,
            wait=args.wait, timeout=args.timeout,
        )
    if cmd == "lab-start-instance":
        return lab.lab_start_instance(args.instance_id, allow_spend=args.allow_spend, max_credits=args.max_credits)
    if cmd == "lab-stop-instance":
        return lab.lab_stop_instance(args.instance_id)
    return None


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "devices":
            out = core.list_devices(online_only=args.online)
        elif args.cmd == "run":
            if args.qasm_file:
                qasm = open(args.qasm_file, encoding="utf-8").read()
            elif args.qasm and args.qasm != "-":
                qasm = args.qasm
            else:  # stdin — the robust path for long circuits / shell quoting.
                # Decode as utf-8-sig so a BOM (some shells inject one) doesn't
                # push "OPENQASM" off char 0 and defeat format detection.
                qasm = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
            if not qasm.strip():
                raise ValueError("no OpenQASM 3 provided (use --qasm, --qasm-file, or stdin)")
            out = core.run_qasm(
                qasm,
                device=args.device,
                shots=args.shots,
                allow_spend=args.allow_spend,
                max_credits=args.max_credits,
                subcategory=args.subcategory,
            )
        elif args.cmd == "qrng":
            out = core.qrng(
                args.bits,
                device=args.device,
                allow_spend=args.allow_spend,
                max_credits=args.max_credits,
                subcategory=args.subcategory,
            )
        elif args.cmd == "recall":
            out = core.quantum_recall(
                _parse_floats(args.amplitudes),
                labels=_parse_strs(args.labels),
                shots=args.shots,
                amplify=not args.no_amplify,
                device=args.device,
                allow_spend=args.allow_spend,
                max_credits=args.max_credits,
                subcategory=args.subcategory,
            )
        elif args.cmd == "mcp":
            from .mcp_server import run_stdio

            run_stdio()
            return 0
        elif args.cmd.startswith("lab-"):
            out = _dispatch_lab(args)
            if out is None:  # pragma: no cover
                raise ValueError(f"unknown lab command {args.cmd}")
        else:  # pragma: no cover
            raise ValueError(f"unknown command {args.cmd}")
        print(json.dumps(out))
        return 0
    except Exception as e:  # surface as JSON so callers can parse failures
        print(json.dumps({"error": str(e), "type": type(e).__name__}))
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
