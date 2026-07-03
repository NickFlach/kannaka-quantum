"""Host-side QuantumOS ↔ NATS swarm bridge (ghostd phase 4, #51; epic #47).

The capstone that closes the /qos loop. QuantumOS boots on a leased instance and
its ring-3 ``swarm_svc`` emits, over the COM2 UART:

  * a Lamport-signed **boot attestation** (a public-key digest, the ASCII
    ``QOS-BOOT|qseed=<hex|none>|ticks=<n>`` message, and a 16 KiB signature), and
  * live **swarm frames** (PING/PONG, DATA request/reply routed to ghostd).

This module speaks that wire protocol on the host: it parses the CRC8-framed
stream, *verifies the attestation* (proving the boot really carried the qseed we
handed it), and relays swarm traffic to/from the kannaka NATS mesh.

Layers, from pure to I/O-bound:

  * **codec** — :func:`crc8`, :func:`encode_frame`, :class:`FrameStream`
    (streaming, resyncs past garbage, tolerates frames split across reads).
  * **attestation** — :func:`verify_attestation` (+ :func:`verify_lamport`,
    :func:`parse_attestation`), byte-compatible with the reference verifier.
  * **byte sources** — :class:`FileByteSource` (QEMU ``-serial file:``,
    receive-only), :class:`TcpByteSource` (QEMU ``-serial tcp:``, two-way),
    :class:`SshTailByteSource` (``tail -f`` the COM2 log over the repo's SSH).
  * **NATS sink** — :class:`NatsSink` interface with a real
    :class:`KannakaCliSink` (shells to the kannaka binary) and a
    :class:`CaptureSink` for tests; publishing stays behind the interface so the
    bridge is exercisable with no live mesh.
  * **orchestration** — :class:`QosBridge`: feed it the COM2 stream; on a
    verified ATTEST it publishes ``KANNAKA.qos.<node>.attest``, DATA frames relay
    to ``.data``; :meth:`QosBridge.request` (two-way sources only) sends a DATA
    frame and awaits the reply.

The frame format, CRC-8/CCITT (poly 0x07, init 0x00, MSB-first) and Lamport
parameters are the contract in ``QuantumOS/user/swarm.h``; the codec + Lamport
verify below are ported from ``QuantumOS/scripts/verify_attestation.py`` and
``scripts/swarm_pingpong.py`` and kept byte-compatible with them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# Wire-protocol constants (mirror user/swarm.h)
# --------------------------------------------------------------------------- #
MAGIC = 0xA5
SWARM_HDR_LEN = 4          # magic + type + len(2)

FRAME_HANDSHAKE = 0x01
FRAME_DATA = 0x02
FRAME_PING = 0x03
FRAME_PONG = 0x04
FRAME_DISCONNECT = 0x05
FRAME_PKDIGEST = 0x10      # 32-byte Lamport public-key commitment
FRAME_ATTEST = 0x11        # attestation ASCII string
FRAME_SIG = 0x12           # signature chunk (concatenated in receive order)

# DATA routing opcodes (payload[0])
SWARM_OP_STATUS = 0x01     # -> ghostd GHOST_STATUS : field R + live
SWARM_OP_RECALL = 0x02     # -> ghostd GHOST_RECALL : payload = 32B probe

# Lamport parameters
LAMPORT_BITS = 256
HASH_LEN = 32
SIG_ELEM = 64              # revealed preimage (32) + complementary pk hash (32)
SIG_LEN = LAMPORT_BITS * SIG_ELEM   # 16384

_FRAME_NAMES = {
    FRAME_HANDSHAKE: "HANDSHAKE", FRAME_DATA: "DATA", FRAME_PING: "PING",
    FRAME_PONG: "PONG", FRAME_DISCONNECT: "DISCONNECT", FRAME_PKDIGEST: "PKDIGEST",
    FRAME_ATTEST: "ATTEST", FRAME_SIG: "SIG",
}


# --------------------------------------------------------------------------- #
# Frame codec (ported from scripts/verify_attestation.py + swarm_pingpong.py)
# --------------------------------------------------------------------------- #
def crc8(data: bytes) -> int:
    """CRC-8/CCITT (poly 0x07, init 0x00, MSB-first) — matches swarm_crc8() in
    user/swarm.h and crc8() in scripts/verify_attestation.py."""
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc


def encode_frame(ftype: int, payload: bytes = b"") -> bytes:
    """Serialize one frame: 0xA5 | type | len:u16(LE) | payload | crc8.
    The CRC covers type + the two length bytes + payload (every byte but the
    magic and the crc), mirroring scripts/swarm_pingpong.py's frame()."""
    if not 0 <= ftype <= 0xFF:
        raise ValueError(f"frame type out of range: {ftype}")
    n = len(payload)
    if n > 0xFFFF:
        raise ValueError(f"payload too long: {n} bytes")
    hdr = bytes([ftype, n & 0xFF, (n >> 8) & 0xFF]) + payload
    return bytes([MAGIC]) + hdr + bytes([crc8(hdr)])


class FrameStream:
    """Incremental, resync-tolerant frame parser.

    Feed it arbitrary byte chunks (as they arrive from a serial/socket read) and
    it yields every complete, CRC-valid ``(ftype, payload)`` — buffering partial
    frames across chunk boundaries and resyncing one byte past any non-magic or
    bad-CRC junk. The resync/CRC logic mirrors parse_frames() in
    scripts/verify_attestation.py and parse() in scripts/swarm_pingpong.py.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.bad_crc = 0

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        """Append ``data`` and return every frame now completable."""
        if data:
            self._buf.extend(data)
        out: list[tuple[int, bytes]] = []
        while True:
            # Drop everything up to the next magic byte (resync past garbage).
            while self._buf and self._buf[0] != MAGIC:
                del self._buf[0]
            if len(self._buf) < SWARM_HDR_LEN:
                return out                       # need magic + type + len(2)
            length = self._buf[2] | (self._buf[3] << 8)
            total = SWARM_HDR_LEN + length + 1   # + crc
            if len(self._buf) < total:
                return out                       # frame split across reads — wait
            body = bytes(self._buf[1:SWARM_HDR_LEN + length])  # type+len+payload
            crc = self._buf[SWARM_HDR_LEN + length]
            if crc8(body) == crc:
                out.append((self._buf[1], bytes(self._buf[SWARM_HDR_LEN:SWARM_HDR_LEN + length])))
                del self._buf[:total]
            else:
                self.bad_crc += 1
                del self._buf[0]                 # resync past this false magic


def parse_frames(blob: bytes) -> tuple[list[tuple[int, bytes]], int]:
    """One-shot parse of a complete capture: ``(frames, bad_crc)``.
    Byte-compatible with parse_frames() in scripts/verify_attestation.py."""
    fs = FrameStream()
    frames = fs.feed(blob)
    return frames, fs.bad_crc


# --------------------------------------------------------------------------- #
# Lamport boot-attestation verification
# (ported from scripts/verify_attestation.py — keep byte-compatible)
# --------------------------------------------------------------------------- #
def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def verify_lamport(message: bytes, pkdigest: bytes, signature: bytes) -> tuple[bool, str]:
    """Rebuild the Lamport public key from the signature and check it against the
    committed digest (and that each revealed preimage hashes to its pk entry).
    Returns ``(ok, reason)``; ``reason`` is empty on success. Ported from
    verify_lamport() in scripts/verify_attestation.py."""
    if len(signature) != SIG_LEN:
        return False, f"signature is {len(signature)} bytes, expected {SIG_LEN}"
    if len(pkdigest) != HASH_LEN:
        return False, f"pk digest is {len(pkdigest)} bytes, expected {HASH_LEN}"

    md = _sha256(message)
    pk_stream = bytearray()
    for i in range(LAMPORT_BITS):
        bit = (md[i >> 3] >> (i & 7)) & 1
        elem = signature[i * SIG_ELEM:(i + 1) * SIG_ELEM]
        preimage = elem[:HASH_LEN]
        comp = elem[HASH_LEN:]
        pk_bit = _sha256(preimage)          # pk[i][bit]
        pk = [None, None]
        pk[bit] = pk_bit
        pk[1 - bit] = comp                  # pk[i][1-bit], from the signature
        pk_stream += pk[0]
        pk_stream += pk[1]

    if _sha256(bytes(pk_stream)) != pkdigest:
        return False, "reconstructed public key does not match committed digest"
    return True, ""


def parse_attestation(msg: str) -> tuple[Optional[str], int]:
    """Parse ``QOS-BOOT|qseed=<hex|none>|ticks=<n>`` -> ``(qseed_hex|None, ticks)``.

    Unlike the reference (which returns the qseed as an int), we keep the hex
    string as attested so the published provenance is exactly what the node
    signed; the numeric compare against the expected qseed is done separately.
    """
    parts = msg.split("|")
    if len(parts) != 3 or parts[0] != "QOS-BOOT":
        raise ValueError(f"malformed attestation: {msg!r}")
    if not parts[1].startswith("qseed=") or not parts[2].startswith("ticks="):
        raise ValueError(f"malformed attestation fields: {msg!r}")
    qseed = parts[1][len("qseed="):]
    ticks = int(parts[2][len("ticks="):])
    qseed_hex = None if qseed == "none" else qseed
    # Validate the hex is parseable now, so a bad value fails loudly here.
    if qseed_hex is not None:
        int(qseed_hex, 16)
    return qseed_hex, ticks


def _qseed_int(value: Optional[str]) -> Optional[int]:
    """Normalize a qseed (hex string, ``'none'``, or ``None``) to an int or None."""
    if value is None:
        return None
    if value.lower() == "none":
        return None
    return int(value, 16)


def verify_parts(
    pkdigest: Optional[bytes],
    attest_msg: Optional[str],
    signature: bytes,
    expected_qseed: Optional[str],
    handshake: bool,
    frame_count: int,
    bad_crc: int,
) -> dict[str, Any]:
    """Verify already-collected attestation parts. Shared by the one-shot
    :func:`verify_attestation` and the streaming :class:`QosBridge`. Returns a
    JSON-serializable verdict; never raises for a verification failure (the
    ``reason`` carries it), only for programmer error.

    ``expected_qseed`` semantics: ``None`` skips the qseed check (signature-only
    verify); ``'none'`` requires a seedless boot; a hex string requires that
    exact qseed. Mirrors the check order in verify_attestation.py's main().
    """
    result: dict[str, Any] = {
        "ok": False,
        "attested_qseed": None,
        "ticks": None,
        "frames": frame_count,
        "bad_crc": bad_crc,
        "handshake": handshake,
        "sig_bytes": len(signature),
        "reason": "",
    }
    if bad_crc:
        result["reason"] = f"{bad_crc} frame(s) with invalid CRC8"
        return result
    if pkdigest is None or attest_msg is None or not signature:
        missing = []
        if pkdigest is None:
            missing.append("pk-digest")
        if attest_msg is None:
            missing.append("attestation")
        if not signature:
            missing.append("signature")
        result["reason"] = "missing frame(s): " + ", ".join(missing)
        return result

    result["attestation"] = attest_msg
    try:
        qseed_hex, ticks = parse_attestation(attest_msg)
    except ValueError as exc:
        result["reason"] = str(exc)
        return result
    result["attested_qseed"] = "none" if qseed_hex is None else qseed_hex
    result["ticks"] = ticks

    ok, reason = verify_lamport(attest_msg.encode("ascii"), pkdigest, signature)
    if not ok:
        result["reason"] = reason
        return result

    if expected_qseed is not None:
        want = _qseed_int(expected_qseed)
        got = _qseed_int(qseed_hex)
        if want != got:
            want_s = "none" if want is None else f"{want:X}"
            got_s = "none" if got is None else f"{got:X}"
            result["reason"] = f"attested qseed {got_s} != expected {want_s}"
            return result

    result["ok"] = True
    return result


def verify_attestation(blob: bytes, expected_qseed: Optional[str] = None) -> dict[str, Any]:
    """Parse a complete COM2 capture and verify its boot attestation.

    ``blob`` is the raw byte stream QuantumOS wrote to COM2 (QEMU
    ``-serial file:``). ``expected_qseed`` is the hex qseed handed on the kernel
    cmdline, ``'none'`` for a seedless boot, or ``None`` to skip the qseed check.
    Returns ``{ok, attested_qseed, ticks, frames, bad_crc, handshake, reason,
    ...}``. Equivalent in outcome to running scripts/verify_attestation.py.
    """
    frames, bad_crc = parse_frames(blob)
    pkdigest: Optional[bytes] = None
    attest_msg: Optional[str] = None
    sig = bytearray()
    handshake = False
    for ftype, payload in frames:
        if ftype == FRAME_HANDSHAKE:
            handshake = True
        elif ftype == FRAME_PKDIGEST:
            pkdigest = bytes(payload)
        elif ftype == FRAME_ATTEST:
            attest_msg = payload.decode("ascii", errors="replace")
        elif ftype == FRAME_SIG:
            sig += payload
    return verify_parts(pkdigest, attest_msg, bytes(sig), expected_qseed,
                        handshake, len(frames), bad_crc)


# --------------------------------------------------------------------------- #
# NATS sink — pluggable so the bridge is testable without a live mesh
# --------------------------------------------------------------------------- #
class NatsSink:
    """Publish interface. ``publish(subject, payload)`` sends one event to the
    mesh; implementations decide the transport."""

    def publish(self, subject: str, payload: dict[str, Any]) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - optional
        pass


class CaptureSink(NatsSink):
    """In-memory sink: records ``{subject, payload}`` events. For tests and dry
    runs — no NATS, no subprocess."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, subject: str, payload: dict[str, Any]) -> None:
        self.events.append({"subject": subject, "payload": payload})


class NullSink(NatsSink):
    """Discards events (``--no-nats``) but counts them, so a run still reports how
    many events it *would* have published."""

    def __init__(self) -> None:
        self.count = 0

    def publish(self, subject: str, payload: dict[str, Any]) -> None:
        self.count += 1


def _default_kannaka_bin() -> str:
    """Resolve the kannaka binary: ``$KANNAKA_BIN`` → the known release path →
    ``kannaka`` on PATH."""
    env = os.environ.get("KANNAKA_BIN")
    if env:
        return env
    known = Path.home() / "Source" / "kannaka-memory" / "target" / "release" / (
        "kannaka.exe" if sys.platform == "win32" else "kannaka"
    )
    if known.exists():
        return str(known)
    return "kannaka"


class KannakaCliSink(NatsSink):
    """Publish qos events onto the kannaka NATS mesh by shelling to the kannaka
    binary.

    NOTE (deviation, documented in the PR): the kannaka CLI has no
    arbitrary-subject publish — ``swarm publish`` only broadcasts a Kuramoto
    phase to a fixed subject. The mesh's real payload-carrying reach is
    ``kannaka inbox send <to> <verb> --arg text=<json>`` (subject
    ``KANNAKA.inbox.<to>``). So this sink routes each qos event through
    ``inbox send``, carrying the intended ``KANNAKA.qos.<node>.<kind>`` subject
    and the full payload inside the JSON body. A subscriber keys off the embedded
    ``subject``. A dedicated ``kannaka swarm publish --subject`` would let this
    publish to the qos subject directly; until then this is the clean, authed
    path. The ``runner`` is injectable so the argv is unit-testable without a
    live mesh.
    """

    def __init__(
        self,
        kannaka_bin: Optional[str] = None,
        target: str = "all",
        timeout: float = 20.0,
        runner=subprocess.run,
    ) -> None:
        self.kannaka_bin = kannaka_bin or _default_kannaka_bin()
        self.target = target
        self.timeout = timeout
        self._runner = runner

    def build_argv(self, subject: str, payload: dict[str, Any]) -> list[str]:
        verb = "qos_" + (subject.rsplit(".", 1)[-1] or "event")
        body = json.dumps({"subject": subject, **payload}, separators=(",", ":"), sort_keys=True)
        # `text=` prefix keeps the value from ever being parsed as a flag.
        return [self.kannaka_bin, "inbox", "send", self.target, verb, "--arg", f"text={body}"]

    def publish(self, subject: str, payload: dict[str, Any]) -> None:
        argv = self.build_argv(subject, payload)
        r = self._runner(argv, capture_output=True, text=True, timeout=self.timeout)
        if r.returncode != 0:
            raise RuntimeError(
                f"kannaka publish failed (rc={r.returncode}): {(r.stderr or '').strip()[:300]}"
            )


# --------------------------------------------------------------------------- #
# Byte sources
# --------------------------------------------------------------------------- #
class ByteSource:
    """A source of COM2 bytes. ``read()`` returns up to ``max_bytes`` (``b''`` on
    EOF/timeout). Two-way sources also implement ``send()``."""

    two_way = False

    def read(self, max_bytes: int = 4096, timeout: Optional[float] = None) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def send(self, data: bytes) -> None:
        raise RuntimeError("this byte source is receive-only (no two-way frames)")

    def close(self) -> None:  # pragma: no cover - optional
        pass


class FileByteSource(ByteSource):
    """Read a COM2 capture written by QEMU ``-serial file:<path>``.

    Receive-only. With ``follow=False`` (default) it reads to EOF once — the mode
    for a completed capture / fixture. With ``follow=True`` it keeps polling for
    appended bytes (a live QEMU still writing), returning ``b''`` only when the
    ``timeout`` since the last byte elapses.
    """

    two_way = False

    def __init__(self, path: str, follow: bool = False, poll: float = 0.1) -> None:
        self.path = path
        self.follow = follow
        self.poll = poll
        self._f = open(path, "rb")

    def read(self, max_bytes: int = 4096, timeout: Optional[float] = None) -> bytes:
        data = self._f.read(max_bytes)
        if data or not self.follow:
            return data
        deadline = None if timeout is None else time.time() + timeout
        while True:
            time.sleep(self.poll)
            data = self._f.read(max_bytes)
            if data:
                return data
            if deadline is not None and time.time() >= deadline:
                return b""

    def close(self) -> None:
        try:
            self._f.close()
        except Exception:
            pass


class TcpByteSource(ByteSource):
    """Two-way COM2 over QEMU ``-serial tcp:HOST:PORT``.

    Connects (retrying until ``connect_timeout``), then ``read()`` pulls with a
    per-call socket timeout and ``send()`` writes frames back into the guest —
    the path for PING/PONG and DATA request/reply.
    """

    two_way = True

    def __init__(self, host: str, port: int, connect_timeout: float = 8.0) -> None:
        deadline = time.time() + connect_timeout
        sock = None
        last_err: Optional[Exception] = None
        while time.time() < deadline:
            try:
                sock = socket.create_connection((host, port), timeout=2.0)
                break
            except OSError as e:  # QEMU's TCP server may not be up yet
                last_err = e
                time.sleep(0.2)
        if sock is None:
            raise RuntimeError(f"could not connect to COM2 tcp {host}:{port}: {last_err}")
        self._sock = sock

    def read(self, max_bytes: int = 4096, timeout: Optional[float] = None) -> bytes:
        self._sock.settimeout(timeout if timeout is not None else 1.0)
        try:
            return self._sock.recv(max_bytes)
        except socket.timeout:
            return b""

    def send(self, data: bytes) -> None:
        self._sock.sendall(data)

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


class SshTailByteSource(ByteSource):
    """Receive-only COM2 stream tailed over SSH from a leased instance.

    Runs ``tail -c +1 -f <remote_path>`` on the instance and reads the COM2 bytes
    off SSH stdout. Reuses the repo's SSH discipline from :mod:`lab` (the
    System32-OpenSSH resolver + pinned-known_hosts options — the Windows path
    gotcha that ``lab.py`` already solves). One-way: attestation verification and
    inbound relay only (the guest's COM2 file is not writable back over tail).
    """

    two_way = False

    def __init__(self, ssh_alias: str, remote_path: str) -> None:
        # Import lazily so the pure codec/verify layers don't drag in lab.py
        # (and its qbraid deps) for callers that only parse bytes.
        from . import lab

        self.ssh_alias = ssh_alias
        self.remote_path = remote_path
        argv = [
            lab._ssh_exe(),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={lab._known_hosts_path()}",
            "-o", f"HostKeyAlias={ssh_alias}",
            ssh_alias,
            "bash -lc " + shlex.quote(f"tail -c +1 -f {shlex.quote(remote_path)}"),
        ]
        self._proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
        )

    def read(self, max_bytes: int = 4096, timeout: Optional[float] = None) -> bytes:
        assert self._proc.stdout is not None
        data = self._proc.stdout.read1(max_bytes) if hasattr(self._proc.stdout, "read1") \
            else self._proc.stdout.read(max_bytes)
        return data or b""

    def close(self) -> None:
        try:
            self._proc.terminate()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Bridge orchestration
# --------------------------------------------------------------------------- #
class QosBridge:
    """Consume a QuantumOS COM2 stream and bridge it to the kannaka NATS mesh.

    Feed raw bytes with :meth:`feed` (or drive a source end-to-end with
    :meth:`run`). On a fully-received, *verified* boot attestation it publishes
    one ``KANNAKA.qos.<node>.attest`` event carrying the qseed provenance; each
    DATA frame relays to ``KANNAKA.qos.<node>.data``. :meth:`request` (two-way
    sources only) sends a DATA frame and awaits the reply.
    """

    def __init__(
        self,
        sink: NatsSink,
        node: Optional[str] = None,
        expected_qseed: Optional[str] = None,
        source: Optional[ByteSource] = None,
    ) -> None:
        self.sink = sink
        self.node = node
        self.expected_qseed = expected_qseed
        self.source = source
        self._stream = FrameStream()
        self._pk: Optional[bytes] = None
        self._attest: Optional[str] = None
        self._sig = bytearray()
        self._handshake = False
        self.attest_result: Optional[dict[str, Any]] = None
        self.attest_published = False
        self.relayed: list[dict[str, Any]] = []

    # -- subject helpers ---------------------------------------------------- #
    def _node_id(self) -> str:
        if self.node:
            return self.node
        if self._attest:
            qseed, _ = parse_attestation(self._attest)
            if qseed:
                return f"qos-{qseed[:16].lower()}"
        return "qos-node"

    def _subject(self, kind: str) -> str:
        return f"KANNAKA.qos.{self._node_id()}.{kind}"

    # -- frame handling ----------------------------------------------------- #
    def feed(self, data: bytes) -> list[dict[str, Any]]:
        """Feed bytes; return the list of events published this call."""
        published: list[dict[str, Any]] = []
        for ftype, payload in self._stream.feed(data):
            ev = self._handle(ftype, payload)
            if ev is not None:
                published.append(ev)
        return published

    def _handle(self, ftype: int, payload: bytes) -> Optional[dict[str, Any]]:
        if ftype == FRAME_HANDSHAKE:
            self._handshake = True
            return None
        if ftype == FRAME_PKDIGEST:
            self._pk = bytes(payload)
            return self._maybe_publish_attest()
        if ftype == FRAME_ATTEST:
            self._attest = payload.decode("ascii", errors="replace")
            return self._maybe_publish_attest()
        if ftype == FRAME_SIG:
            self._sig += payload
            return self._maybe_publish_attest()
        if ftype == FRAME_DATA:
            return self._relay_data(payload)
        # PING/PONG/DISCONNECT and anything else: record locally, not published.
        return None

    def _maybe_publish_attest(self) -> Optional[dict[str, Any]]:
        """Once pk + attestation + a full-length signature are in, verify and
        publish exactly one attest event."""
        if self.attest_published:
            return None
        if self._pk is None or self._attest is None or len(self._sig) < SIG_LEN:
            return None
        result = verify_parts(
            self._pk, self._attest, bytes(self._sig[:SIG_LEN]), self.expected_qseed,
            self._handshake, self._stream_frame_count(), self._stream.bad_crc,
        )
        self.attest_result = result
        self.attest_published = True  # publish once, verified or not (verdict travels)
        payload = {
            "node": self._node_id(),
            "ok": result["ok"],
            "qseed": result.get("attested_qseed"),
            "ticks": result.get("ticks"),
            "attestation": result.get("attestation"),
            "sig_bytes": result.get("sig_bytes"),
            "reason": result.get("reason", ""),
            "ts": time.time(),
        }
        subject = self._subject("attest")
        self.sink.publish(subject, payload)
        event = {"subject": subject, "payload": payload}
        self.relayed.append(event)
        return event

    def _stream_frame_count(self) -> int:
        # Not tracked precisely for streaming; the attest verdict doesn't need an
        # exact count, so report the parts we care about.
        n = 0
        n += 1 if self._pk is not None else 0
        n += 1 if self._attest is not None else 0
        n += 1 if self._handshake else 0
        return n

    def _relay_data(self, payload: bytes) -> Optional[dict[str, Any]]:
        opcode = payload[0] if payload else None
        event_payload = {
            "node": self._node_id(),
            "opcode": opcode,
            "op": {SWARM_OP_STATUS: "status", SWARM_OP_RECALL: "recall"}.get(opcode, "unknown"),
            "data": payload.hex(),
            "ts": time.time(),
        }
        subject = self._subject("data")
        self.sink.publish(subject, event_payload)
        event = {"subject": subject, "payload": event_payload}
        self.relayed.append(event)
        return event

    # -- driving a live source --------------------------------------------- #
    def run(self, timeout: float = 15.0, until_attest: bool = False) -> dict[str, Any]:
        """Pull from ``self.source`` and feed the bridge until ``timeout`` (or,
        with ``until_attest``, until the attestation is published). Returns a
        summary dict. Requires a source."""
        if self.source is None:
            raise RuntimeError("QosBridge.run needs a source")
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            chunk = self.source.read(4096, timeout=min(1.0, max(0.0, remaining)))
            if chunk:
                self.feed(chunk)
            elif not self.source.two_way:
                # Receive-only source returned EOF/empty: for a static capture
                # that means we're done; for follow-mode the timeout governs.
                if not getattr(self.source, "follow", False):
                    break
            if until_attest and self.attest_published:
                break
        return self.summary()

    def request(self, op: int, payload: bytes = b"", timeout: float = 8.0) -> dict[str, Any]:
        """Send a DATA request (opcode ``op`` + ``payload``) and await the DATA
        reply. Two-way sources only. Returns the decoded reply frame."""
        if self.source is None or not self.source.two_way:
            raise RuntimeError("request() needs a two-way (tcp) source")
        self.source.send(encode_frame(FRAME_DATA, bytes([op]) + payload))
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self.source.read(4096, timeout=min(1.0, deadline - time.time()))
            if not chunk:
                continue
            for ftype, reply in self._stream.feed(chunk):
                # Keep bridging attest/relay frames seen along the way.
                self._handle(ftype, reply)
                if ftype == FRAME_DATA and reply and reply[0] == op:
                    return {"ok": True, "op": op, "reply": reply.hex(), "len": len(reply)}
        return {"ok": False, "op": op, "reason": f"no DATA reply for opcode {op} within {timeout}s"}

    def ping(self, timeout: float = 5.0) -> dict[str, Any]:
        """Send a PING and confirm a PONG (two-way sources only)."""
        if self.source is None or not self.source.two_way:
            raise RuntimeError("ping() needs a two-way (tcp) source")
        self.source.send(encode_frame(FRAME_PING, b"hi"))
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self.source.read(4096, timeout=min(1.0, deadline - time.time()))
            if not chunk:
                continue
            for ftype, reply in self._stream.feed(chunk):
                self._handle(ftype, reply)
                if ftype == FRAME_PONG:
                    return {"ok": True, "pong": reply.hex()}
        return {"ok": False, "reason": f"no PONG within {timeout}s"}

    def summary(self) -> dict[str, Any]:
        return {
            "node": self._node_id(),
            "attestation": self.attest_result,
            "published": len(self.relayed),
            "bad_crc": self._stream.bad_crc,
        }


# --------------------------------------------------------------------------- #
# Command entry points (JSON-dict returning, like the rest of the package)
# --------------------------------------------------------------------------- #
def _make_sink(nats: bool, kannaka_bin: Optional[str], target: str) -> NatsSink:
    return KannakaCliSink(kannaka_bin=kannaka_bin, target=target) if nats else NullSink()


def _make_source(source: str, path: Optional[str], host: Optional[str],
                 port: Optional[int], alias: Optional[str], follow: bool) -> ByteSource:
    if source == "file":
        if not path:
            raise RuntimeError("--source file needs --path")
        return FileByteSource(path, follow=follow)
    if source == "tcp":
        if not host or not port:
            raise RuntimeError("--source tcp needs --host and --port")
        return TcpByteSource(host, port)
    if source == "ssh":
        if not alias or not path:
            raise RuntimeError("--source ssh needs --alias and --path (remote COM2 log path)")
        return SshTailByteSource(alias, path)
    raise RuntimeError(f"unknown source {source!r} (want file|tcp|ssh)")


def qos_bridge_verify(path: str, expected_qseed: Optional[str] = None) -> dict[str, Any]:
    """Verify a boot attestation from a COM2 capture file (no NATS). Mirrors
    ``scripts/verify_attestation.py`` but returns a JSON-serializable verdict."""
    with open(path, "rb") as f:
        blob = f.read()
    result = verify_attestation(blob, expected_qseed=expected_qseed)
    result["path"] = path
    result["bytes"] = len(blob)
    return result


def qos_bridge_relay(
    source: str = "file",
    path: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    alias: Optional[str] = None,
    node: Optional[str] = None,
    expected_qseed: Optional[str] = None,
    nats: bool = True,
    once: bool = False,
    timeout: float = 15.0,
    kannaka_bin: Optional[str] = None,
    target: str = "all",
) -> dict[str, Any]:
    """Bridge a COM2 stream to the mesh. ``once`` verifies one attestation and
    exits; otherwise it streams for ``timeout`` seconds relaying DATA too.
    Returns the bridge summary (incl. the attestation verdict) plus how many
    events were published."""
    follow = not once  # a live stream keeps growing; a one-shot capture doesn't
    src = _make_source(source, path, host, port, alias, follow)
    sink = _make_sink(nats, kannaka_bin, target)
    bridge = QosBridge(sink=sink, node=node, expected_qseed=expected_qseed, source=src)
    try:
        summary = bridge.run(timeout=timeout, until_attest=once)
    finally:
        src.close()
        sink.close()
    summary["nats"] = nats
    if isinstance(sink, NullSink):
        summary["would_publish"] = sink.count
    return summary
