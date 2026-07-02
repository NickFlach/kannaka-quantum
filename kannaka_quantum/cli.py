"""JSON command-line bridge — the surface the Kannaka coding agent shells out
to. Every subcommand prints a single JSON object to stdout (errors included),
so a caller can parse it directly.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from . import bell
from . import bench
from . import core
from . import entropy
from . import lab
from . import qubo


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

    # --- entropy reservoir + DRBG (real-device-only) -----------------------
    hv = sub.add_parser("harvest", help="harvest real-QPU bits into the entropy reservoir")
    hv.add_argument("--bits", type=int, default=entropy.DEFAULT_HARVEST_BITS)
    hv.add_argument("--device", default=entropy.DEFAULT_HARVEST_DEVICE,
                    help="real per-shot QPU (simulator refused). Default: " + entropy.DEFAULT_HARVEST_DEVICE)
    _add_spend_opts(hv)

    sub.add_parser("qrng-status", help="entropy reservoir level, provenance, refill cost")

    dw = sub.add_parser("qrng-draw", help="draw bits from the reservoir (raw, or HMAC-DRBG --expand)")
    dw.add_argument("--bits", type=int, required=True)
    dw.add_argument("--expand", action="store_true",
                    help="seed an HMAC-DRBG from reservoir bits and expand (else return raw reservoir bits)")

    rc = sub.add_parser("recall", help="resonance recall as amplitude amplification")
    rc.add_argument("--amplitudes", required=True, help="comma-separated or JSON list of resonances")
    rc.add_argument("--labels", help="comma-separated or JSON list of labels")
    rc.add_argument("--shots", type=int, default=1024)
    rc.add_argument("--no-amplify", action="store_true", help="skip amplitude amplification")
    rc.add_argument("--device", default=core.DEFAULT_DEVICE, help=_DEVICE_HELP)
    _add_spend_opts(rc)

    bn = sub.add_parser(
        "bench",
        help="recall-correspondence benchmark over a kannaka-recall-bench/1 corpus",
    )
    bn.add_argument("--scenarios", required=True, help="path to a kannaka-recall-bench/1 corpus JSON")
    bn.add_argument(
        "--device",
        default=core.LOCAL_DEVICE,
        help="backend (default: local state-vector, $0/offline). Use "
        "'qbraid:qbraid:sim:qir-sv' for the hosted free simulator.",
    )
    bn.add_argument("--shots", type=int, default=1024)
    bn.add_argument("--limit", type=int, default=None, help="only run the first N scenarios (e.g. a HW subset)")
    bn.add_argument("--no-amplify", action="store_true", help="skip amplitude amplification")
    bn.add_argument("--out", default=None, help="write the full result JSON to this path")
    bn.add_argument("--baseline", default=None, help="baseline JSON to gate against (regression = agreement drop > threshold)")
    bn.add_argument(
        "--regression-threshold",
        type=float,
        default=bench.DEFAULT_REGRESSION_POINTS,
        help="max allowed agreement-rate drop, in points (default 2.0)",
    )
    bn.add_argument(
        "--update-baseline",
        action="store_true",
        help="write this run as the baseline (to --baseline) and skip the gate",
    )
    _add_spend_opts(bn)

    qb = sub.add_parser(
        "qubo",
        help="solve a kannaka-qubo/1 problem with QAOA (ADR-0038 consolidation solver)",
    )
    qb.add_argument("--problem-file", help="path to a kannaka-qubo/1 JSON file ('-' or omit reads stdin)")
    qb.add_argument("--device", default=core.LOCAL_DEVICE, help=_DEVICE_HELP)
    qb.add_argument("--shots", type=int, default=1024)
    qb.add_argument("--max-p", type=int, default=3, help="max QAOA depth to try (p=1..max-p, default 3)")
    _add_spend_opts(qb)

    bl = sub.add_parser("bell", help="CHSH inequality test (S ~= 2sqrt(2) on the simulator)")
    bl.add_argument("--device", default=core.LOCAL_DEVICE, help=_DEVICE_HELP)
    bl.add_argument("--shots", type=int, default=4096, help="shots per CHSH setting (default 4096)")
    _add_spend_opts(bl)

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
    up.add_argument("--max-minutes", type=int, default=lab.DEFAULT_LEASE_MINUTES, help="lease wall-time before lab-reap stops it")
    _add_lab_spend_opts(up)

    dn = sub.add_parser("lab-compute-down", help="stop the Lab server (preserves disk)")
    dn.add_argument("--cluster", default=None)

    pv = sub.add_parser("lab-provision-instance", help="provision a new on-demand instance (PAID)")
    pv.add_argument("--profile", required=True)
    pv.add_argument("--wait", action="store_true")
    pv.add_argument("--timeout", type=float, default=None)
    pv.add_argument("--max-minutes", type=int, default=lab.DEFAULT_LEASE_MINUTES, help="lease wall-time before lab-reap stops it")
    _add_lab_spend_opts(pv)

    si = sub.add_parser("lab-start-instance", help="resume a stopped instance (PAID)")
    si.add_argument("--instance-id", required=True)
    si.add_argument("--max-minutes", type=int, default=lab.DEFAULT_LEASE_MINUTES, help="lease wall-time before lab-reap stops it")
    _add_lab_spend_opts(si)

    sp = sub.add_parser("lab-stop-instance", help="stop (pause) an instance (preserves disk)")
    sp.add_argument("--instance-id", required=True)

    tp = sub.add_parser("lab-terminate-instance", help="TERMINATE an instance (frees disk, stops ALL billing)")
    tp.add_argument("--instance-id", required=True)

    rp = sub.add_parser("lab-reap", help="stop instances/servers past their lease (cron/timer-friendly)")
    rp.add_argument("--dry-run", action="store_true", help="report what would be stopped/terminated without doing it")
    rp.add_argument(
        "--terminate-stopped", action="store_true",
        help="DESTRUCTIVE: also TERMINATE (delete disk, stop ALL billing) instances stopped past their lease + "
        "grace. A stopped instance keeps billing stopped_credits_per_min for its disk; this frees it but DESTROYS "
        "the disk. Default off; also enabled by KANNAKA_REAP_TERMINATE=1.",
    )
    rp.add_argument(
        "--terminate-grace-minutes", type=int, default=None,
        help="only terminate instances stopped past lease by this margin (default 360 = 6h; env "
        "KANNAKA_REAP_TERMINATE_GRACE_MIN)",
    )

    # --- remote agents (run a coding agent on a provisioned instance) -------
    sc = sub.add_parser("lab-ssh-configure", help="configure SSH to an instance, return its alias")
    sc.add_argument("--instance-id", required=True)

    al = sub.add_parser("lab-agent-launch", help="launch a coding agent on a remote instance via SSH")
    al.add_argument("--ssh-alias", required=True)
    al.add_argument("--tool", default="claude", help="claude | codex | opencode")
    al.add_argument("--instructions", default=None)
    al.add_argument("--cwd", default=None)
    al.add_argument("--name", default=None)
    al.add_argument("--agent-type", default=None)
    al.add_argument("--tags", default=None, help="JSON list or CSV")
    al.add_argument("--allow-unleased", action="store_true", help="override the lease GATE (launch on an unleased instance)")

    aL = sub.add_parser("lab-agent-list", help="list remote agents on an instance")
    aL.add_argument("--ssh-alias", required=True)
    aL.add_argument("--include-stopped", action="store_true")

    ar = sub.add_parser("lab-agent-read", help="read a remote agent's terminal output")
    ar.add_argument("--ssh-alias", required=True)
    ar.add_argument("--session-id", required=True)
    ar.add_argument("--lines", type=int, default=50)

    asnd = sub.add_parser("lab-agent-send", help="send text to a remote agent")
    asnd.add_argument("--ssh-alias", required=True)
    asnd.add_argument("--session-id", required=True)
    asnd.add_argument("--text", required=True)

    aset = sub.add_parser("lab-agent-setup", help="prep a remote instance for autonomous claude (key+onboarding+model)")
    aset.add_argument("--ssh-alias", required=True)
    aset.add_argument("--provider", default="anthropic")
    aset.add_argument("--model", default="claude-sonnet-4-6")
    aset.add_argument("--api-key", default=None)
    aset.add_argument("--i-know", action="store_true", help="override the same-as-primary-key refusal (upload your primary ANTHROPIC_API_KEY)")

    atd = sub.add_parser("lab-agent-teardown", help="delete the uploaded key from a remote instance + rotation reminder")
    atd.add_argument("--ssh-alias", required=True)

    ex = sub.add_parser("lab-exec", help="run a shell command on a provisioned instance over SSH")
    ex.add_argument("--ssh-alias", required=True)
    ex.add_argument("--command", required=True)
    ex.add_argument("--timeout-secs", type=int, default=90)

    qb = sub.add_parser("lab-qos-boot", help="boot QuantumOS in QEMU on a provisioned instance (tmux serial console)")
    qb.add_argument("--ssh-alias", required=True)
    qb.add_argument("--repo", default=None)
    qb.add_argument("--ref", default=None)
    qb.add_argument("--session", default="qos")
    qb.add_argument("--fresh", action="store_true", help="kill an existing session and reboot from a rebuilt kernel")
    qb.add_argument("--allow-unleased", action="store_true")
    qb.add_argument("--timeout-secs", type=int, default=540)
    qb.add_argument("--qseed", default=None,
                    help="kernel boot entropy: 'reservoir' (draw 64 raw QPU bits locally) or <=16 hex digits")
    qb.add_argument("--graphical", action="store_true",
                    help="boot with a VGA framebuffer over VNC (paused) + install noVNC; watch with lab-watch")
    qb.add_argument("--web-port", type=int, default=lab.QOS_DEFAULT_WEB_PORT, help="websockify web port for --graphical")
    qb.add_argument("--monitor-port", type=int, default=lab.QOS_DEFAULT_MONITOR_PORT, help="QEMU TCP monitor port for --graphical")

    wt = sub.add_parser("lab-watch", help="open the graphical QuantumOS watch (SSH -L tunnel + browser + resume)")
    wt.add_argument("--ssh-alias", required=True)
    wt.add_argument("--session", default="qos")
    wt.add_argument("--web-port", type=int, default=lab.QOS_DEFAULT_WEB_PORT)
    wt.add_argument("--monitor-port", type=int, default=lab.QOS_DEFAULT_MONITOR_PORT)
    wt.add_argument("--local-port", type=int, default=None)
    wt.add_argument("--no-resume", action="store_true", help="don't send monitor 'cont' (VM already running)")
    wt.add_argument("--no-browser", action="store_true", help="set up the tunnel but don't open a browser")

    qr = sub.add_parser("lab-qos-resume", help="resume a paused graphical QuantumOS VM (monitor cont)")
    qr.add_argument("--ssh-alias", required=True)
    qr.add_argument("--monitor-port", type=int, default=lab.QOS_DEFAULT_MONITOR_PORT)

    sb = sub.add_parser("ssh-bridge", help="Windows-safe websocket<->stdio SSH ProxyCommand shim")
    sb.add_argument("url")
    sb.add_argument("--token", default=None)
    sb.add_argument("--ping-interval", type=float, default=30.0)

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
            cluster=args.cluster, wait=args.wait, timeout=args.timeout, max_minutes=args.max_minutes,
        )
    if cmd == "lab-compute-down":
        return lab.lab_compute_down(cluster=args.cluster)
    if cmd == "lab-provision-instance":
        return lab.lab_provision_instance(
            args.profile, allow_spend=args.allow_spend, max_credits=args.max_credits,
            wait=args.wait, timeout=args.timeout, max_minutes=args.max_minutes,
        )
    if cmd == "lab-start-instance":
        return lab.lab_start_instance(
            args.instance_id, allow_spend=args.allow_spend, max_credits=args.max_credits, max_minutes=args.max_minutes,
        )
    if cmd == "lab-stop-instance":
        return lab.lab_stop_instance(args.instance_id)
    if cmd == "lab-terminate-instance":
        return lab.lab_terminate_instance(args.instance_id)
    if cmd == "lab-reap":
        return lab.lab_reap(
            dry_run=args.dry_run,
            terminate_stopped=args.terminate_stopped,
            terminate_grace_minutes=args.terminate_grace_minutes,
        )
    if cmd == "lab-ssh-configure":
        return lab.lab_ssh_configure(args.instance_id)
    if cmd == "lab-agent-launch":
        return lab.lab_agent_launch(
            args.ssh_alias,
            tool=args.tool,
            instructions=args.instructions,
            cwd=args.cwd,
            name=args.name,
            agent_type=args.agent_type,
            tags=_parse_json_arg(args.tags),
            allow_unleased=args.allow_unleased,
        )
    if cmd == "lab-agent-list":
        return lab.lab_agent_list(args.ssh_alias, include_stopped=args.include_stopped)
    if cmd == "lab-agent-read":
        return lab.lab_agent_read(args.ssh_alias, args.session_id, lines=args.lines)
    if cmd == "lab-agent-send":
        return lab.lab_agent_send(args.ssh_alias, args.session_id, args.text)
    if cmd == "lab-agent-setup":
        return lab.lab_agent_setup(
            args.ssh_alias, provider=args.provider, model=args.model, api_key=args.api_key, i_know=args.i_know,
        )
    if cmd == "lab-agent-teardown":
        return lab.lab_agent_teardown(args.ssh_alias)
    if cmd == "lab-exec":
        return lab.lab_exec(args.ssh_alias, args.command, timeout_secs=args.timeout_secs)
    if cmd == "lab-qos-boot":
        return lab.lab_qos_boot(
            args.ssh_alias,
            repo=args.repo or lab.QOS_DEFAULT_REPO,
            ref=args.ref,
            session=args.session,
            fresh=args.fresh,
            allow_unleased=args.allow_unleased,
            timeout_secs=args.timeout_secs,
            qseed=args.qseed,
            graphical=args.graphical,
            web_port=args.web_port,
            monitor_port=args.monitor_port,
        )
    if cmd == "lab-watch":
        return lab.lab_watch(
            args.ssh_alias,
            session=args.session,
            web_port=args.web_port,
            monitor_port=args.monitor_port,
            local_port=args.local_port,
            resume=not args.no_resume,
            open_browser=not args.no_browser,
        )
    if cmd == "lab-qos-resume":
        return lab.lab_qos_resume(args.ssh_alias, monitor_port=args.monitor_port)
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
        elif args.cmd == "harvest":
            out = entropy.harvest(
                args.bits,
                device=args.device,
                allow_spend=args.allow_spend,
                max_credits=args.max_credits,
                subcategory=args.subcategory,
            )
        elif args.cmd == "qrng-status":
            out = entropy.status()
        elif args.cmd == "qrng-draw":
            out = entropy.draw(args.bits, expand=args.expand)
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
        elif args.cmd == "qubo":
            if args.problem_file and args.problem_file != "-":
                problem_text = open(args.problem_file, encoding="utf-8").read()
            else:  # stdin — the JSON-CLI boundary the Rust SubprocessSolver uses.
                problem_text = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
            if not problem_text.strip():
                raise ValueError("no kannaka-qubo/1 problem provided (use --problem-file or stdin)")
            out = qubo.solve(
                qubo.load_problem(problem_text),
                device=args.device,
                shots=args.shots,
                max_p=args.max_p,
                allow_spend=args.allow_spend,
                max_credits=args.max_credits,
                subcategory=args.subcategory,
            )
        elif args.cmd == "bell":
            out = bell.chsh(
                device=args.device,
                shots=args.shots,
                allow_spend=args.allow_spend,
                max_credits=args.max_credits,
                subcategory=args.subcategory,
            )
        elif args.cmd == "bench":
            # Prints the result JSON like every other command, but the exit code
            # carries the regression verdict so CI can gate a PR on it.
            result, code = bench.bench_command(
                scenarios=args.scenarios,
                device=args.device,
                shots=args.shots,
                amplify=not args.no_amplify,
                limit=args.limit,
                allow_spend=args.allow_spend,
                max_credits=args.max_credits,
                subcategory=args.subcategory,
                out=args.out,
                baseline=args.baseline,
                regression_threshold=args.regression_threshold,
                update_baseline=args.update_baseline,
            )
            print(json.dumps(result))
            return code
        elif args.cmd == "mcp":
            from .mcp_server import run_stdio

            run_stdio()
            return 0
        elif args.cmd == "ssh-bridge":
            # Raw stdio passthrough (no JSON) — it IS the SSH transport.
            from .ssh_bridge import run_ssh_bridge

            return run_ssh_bridge(args.url, token=args.token, ping_interval=args.ping_interval)
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
