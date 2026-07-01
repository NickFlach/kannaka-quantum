"""Windows-safe WebSocket <-> stdio bridge used as an SSH ProxyCommand.

qBraid provisions instances reachable over a WebSocket-tunnelled SSH. Its own
bridge (``python -m qbraid_core.services.compute.ssh bridge``) sets up stdin
with ``loop.connect_read_pipe(sys.stdin)``, which raises ``OSError [WinError 6]
The handle is invalid`` under the Windows asyncio Proactor loop — so SSH (and
thus the lab_agent_* remote tools) fails on Windows.

This shim does the identical job (pump stdin->ws, ws->stdout) but reads stdin
on a worker thread (``asyncio.to_thread``), which works on every platform.
``lab_ssh_configure`` rewrites the generated ProxyCommand to call this on
Windows. It can also be invoked directly:

    python -m kannaka_quantum ssh-bridge "wss://.../sshd/" --token "qbr-at_..."
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Optional

#: 64 KiB — a single read1 returns whatever is already buffered (interactive
#: streaming) up to this, so the tunnel stays responsive.
_CHUNK = 65536

#: Env alternative to ``--token``. qBraid's generated ProxyCommand passes the
#: WebSocket auth token as an argv value, which is visible in the local process
#: list (``ps`` / Task Manager) on a shared host. Callers that invoke the bridge
#: directly can pass the token here instead to keep it out of argv.
_TOKEN_ENV = "KANNAKA_SSH_BRIDGE_TOKEN"


def _resolve_bridge_token(token: Optional[str]) -> Optional[str]:
    """Prefer an explicit ``--token``; else fall back to ``KANNAKA_SSH_BRIDGE_TOKEN``.

    An explicit token always wins (the qBraid-generated ProxyCommand relies on
    it). The env path exists so a direct invocation can avoid putting the secret
    on the command line.
    """
    if token:
        return token
    env = os.environ.get(_TOKEN_ENV)
    return env.strip() if env else None


async def _bridge(url: str, token: Optional[str], ping_interval: float) -> None:
    from websockets.asyncio.client import connect as ws_connect

    kwargs: dict = {"ping_interval": ping_interval}
    if token:
        kwargs["additional_headers"] = {"Authorization": f"token {token}"}

    async with ws_connect(url, **kwargs) as ws:
        stdin = sys.stdin.buffer
        stdout = sys.stdout.buffer

        async def pump_stdin() -> None:
            try:
                while True:
                    # Thread-based blocking read — the Windows-safe replacement
                    # for loop.connect_read_pipe(sys.stdin).
                    data = await asyncio.to_thread(stdin.read1, _CHUNK)
                    if not data:
                        break
                    await ws.send(data)
            except Exception:
                pass

        async def pump_stdout() -> None:
            try:
                async for message in ws:
                    if isinstance(message, str):
                        message = message.encode()
                    stdout.write(message)
                    stdout.flush()
            except Exception:
                pass

        t_in = asyncio.create_task(pump_stdin())
        t_out = asyncio.create_task(pump_stdout())
        _done, pending = await asyncio.wait({t_in, t_out}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()


def run_ssh_bridge(url: str, token: Optional[str] = None, ping_interval: float = 30.0) -> int:
    """Entry point for the ``ssh-bridge`` CLI subcommand (raw stdio, no JSON)."""
    token = _resolve_bridge_token(token)
    try:
        asyncio.run(_bridge(url, token, ping_interval))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as e:  # surface to ssh's stderr
        print(f"ssh-bridge error: {e}", file=sys.stderr)
        return 1
