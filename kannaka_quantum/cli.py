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

    sub.add_parser("mcp", help="launch the MCP server (stdio)")
    return p


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
        else:  # pragma: no cover
            raise ValueError(f"unknown command {args.cmd}")
        print(json.dumps(out))
        return 0
    except Exception as e:  # surface as JSON so callers can parse failures
        print(json.dumps({"error": str(e), "type": type(e).__name__}))
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
