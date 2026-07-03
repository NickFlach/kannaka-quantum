"""Tests for the QuantumOS <-> NATS swarm bridge (ghostd phase 4, #51).

Two layers, both offline (no live mesh, no provisioning):

  * codec + attestation verify are checked against known CRC vectors, frame
    round-trips, resync/partial tolerance, AND the *real* golden COM2 captures
    committed under fixtures/ (booted from the merged QuantumOS with
    `make ci-smoke-swarm`-equivalent QEMU invocations).
  * the bridge relay + NATS sink use a CaptureSink / a fake subprocess runner,
    so no NATS connection is ever opened.
"""

import json
from pathlib import Path

import pytest

from kannaka_quantum import qos_bridge as qb

FIXTURES = Path(__file__).parent / "fixtures"
QSEED_CAP = FIXTURES / "qos_com2_attest_qseed.bin"
NOQSEED_CAP = FIXTURES / "qos_com2_attest_noqseed.bin"
QSEED = "DEADBEEFCAFEBABE"


# --------------------------------------------------------------------------- #
# CRC8 — known vectors + reference agreement
# --------------------------------------------------------------------------- #
def test_crc8_known_vectors():
    # CRC-8/CCITT (poly 0x07, init 0x00): the canonical check value for b"123456789".
    assert qb.crc8(b"123456789") == 0xF4
    assert qb.crc8(b"") == 0x00
    assert qb.crc8(b"\x00") == 0x00
    # A single 0x01 through poly 0x07: 8 shifts of 0x01 -> 0x07.
    assert qb.crc8(b"\x01") == 0x07


def test_crc8_matches_reference_over_frame_bodies():
    # For every frame type, the crc our encoder emits equals crc8 over type+len+payload.
    for ftype in (qb.FRAME_HANDSHAKE, qb.FRAME_DATA, qb.FRAME_ATTEST, qb.FRAME_SIG):
        payload = bytes(range(17))
        f = qb.encode_frame(ftype, payload)
        body = f[1:-1]  # strip magic + crc
        assert f[-1] == qb.crc8(body)


# --------------------------------------------------------------------------- #
# Frame round-trip, resync, partial tolerance
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ftype", [
    qb.FRAME_HANDSHAKE, qb.FRAME_DATA, qb.FRAME_PING, qb.FRAME_PONG,
    qb.FRAME_DISCONNECT, qb.FRAME_PKDIGEST, qb.FRAME_ATTEST, qb.FRAME_SIG,
])
def test_frame_roundtrip_every_type(ftype):
    payload = bytes([ftype]) + b"payload-bytes-\x00\xff" * 3
    enc = qb.encode_frame(ftype, payload)
    frames, bad = qb.parse_frames(enc)
    assert bad == 0
    assert frames == [(ftype, payload)]


def test_empty_payload_roundtrip():
    enc = qb.encode_frame(qb.FRAME_PING, b"")
    frames, bad = qb.parse_frames(enc)
    assert frames == [(qb.FRAME_PING, b"")] and bad == 0


def test_resync_past_leading_and_interior_garbage():
    a = qb.encode_frame(qb.FRAME_DATA, b"one")
    b = qb.encode_frame(qb.FRAME_DATA, b"two")
    # Non-magic garbage before, between, and after — the common case (line noise
    # that never looks like a frame header).
    stream = b"\x00\x01\x02" + a + b"\x00\xffjunk\x11" + b + b"\xff\xff"
    frames, _ = qb.parse_frames(stream)
    assert (qb.FRAME_DATA, b"one") in frames
    assert (qb.FRAME_DATA, b"two") in frames


def test_resync_past_stray_magic_with_fully_present_bad_frame():
    # A stray 0xA5 whose claimed short frame is fully buffered fails CRC and the
    # parser advances one byte to recover the real frame that follows. (A stray
    # magic with a huge length field stalls until more bytes arrive — the same
    # bounded-buffer behavior as the reference verify_attestation.py.)
    real = qb.encode_frame(qb.FRAME_DATA, b"real")
    stray = b"\xA5\x02\x03\x00aaa\x00"  # magic + tiny len=3 + bytes + wrong crc
    frames, bad = qb.parse_frames(stray + real)
    assert (qb.FRAME_DATA, b"real") in frames
    assert bad >= 1


def test_bad_crc_is_counted_and_resynced():
    good = qb.encode_frame(qb.FRAME_DATA, b"good")
    bad = bytearray(qb.encode_frame(qb.FRAME_DATA, b"bad!"))
    bad[-1] ^= 0xFF  # corrupt the CRC
    frames, bad_crc = qb.parse_frames(bytes(bad) + good)
    assert bad_crc >= 1
    assert (qb.FRAME_DATA, b"good") in frames


def test_partial_frame_tolerated_across_feeds():
    enc = qb.encode_frame(qb.FRAME_ATTEST, b"QOS-BOOT|qseed=none|ticks=1")
    fs = qb.FrameStream()
    out = []
    # Feed one byte at a time — the parser must buffer until the frame completes.
    for i in range(len(enc)):
        out += fs.feed(enc[i:i + 1])
    assert out == [(qb.FRAME_ATTEST, b"QOS-BOOT|qseed=none|ticks=1")]
    assert fs.bad_crc == 0


# --------------------------------------------------------------------------- #
# Attestation verification against the REAL golden captures
# --------------------------------------------------------------------------- #
def test_golden_captures_exist():
    assert QSEED_CAP.exists() and NOQSEED_CAP.exists(), "run the capture step (see PR) to regenerate fixtures"


def test_verify_real_qseed_capture_passes():
    blob = QSEED_CAP.read_bytes()
    res = qb.verify_attestation(blob, expected_qseed=QSEED)
    assert res["ok"], res
    assert res["attested_qseed"] == QSEED
    assert res["bad_crc"] == 0
    assert res["ticks"] is not None


def test_verify_real_noqseed_capture_passes():
    blob = NOQSEED_CAP.read_bytes()
    res = qb.verify_attestation(blob, expected_qseed="none")
    assert res["ok"], res
    assert res["attested_qseed"] == "none"
    assert res["bad_crc"] == 0


def test_verify_skips_qseed_when_expected_is_none():
    # expected_qseed=None => signature-only verify, no cmdline compare.
    res = qb.verify_attestation(QSEED_CAP.read_bytes(), expected_qseed=None)
    assert res["ok"], res
    assert res["attested_qseed"] == QSEED


def test_verify_fails_on_bitflipped_signature():
    blob = bytearray(QSEED_CAP.read_bytes())
    # Flip a bit inside a SIG frame's payload. SIG frames are the bulk of the
    # capture; find the first one and corrupt a payload byte (then fix its CRC so
    # the failure is a *signature* failure, not merely a CRC failure).
    # Locate a SIG frame offset by re-scanning the raw bytes.
    i = 0
    sig_payload_off = None
    while i < len(blob):
        if blob[i] != qb.MAGIC:
            i += 1
            continue
        if i + 4 > len(blob):
            break
        ftype = blob[i + 1]
        length = blob[i + 2] | (blob[i + 3] << 8)
        end = i + 4 + length
        if end + 1 > len(blob):
            break
        if qb.crc8(bytes(blob[i + 1:end])) == blob[end] and ftype == qb.FRAME_SIG and length > 0:
            sig_payload_off = i + 4
            frame_body_start = i + 1
            frame_body_end = end
            crc_off = end
            break
        i = end + 1
    assert sig_payload_off is not None, "no SIG frame found in capture"
    blob[sig_payload_off] ^= 0x01
    # Recompute this frame's CRC so it parses cleanly (isolates the sig failure).
    blob[crc_off] = qb.crc8(bytes(blob[frame_body_start:frame_body_end]))
    res = qb.verify_attestation(bytes(blob), expected_qseed=QSEED)
    assert not res["ok"]
    assert "public key" in res["reason"]


def test_verify_fails_on_qseed_mismatch():
    res = qb.verify_attestation(QSEED_CAP.read_bytes(), expected_qseed="0BADC0DE")
    assert not res["ok"]
    assert "qseed" in res["reason"]


def test_verify_fails_on_missing_frames():
    res = qb.verify_attestation(b"", expected_qseed="none")
    assert not res["ok"]
    assert "missing" in res["reason"]


# --------------------------------------------------------------------------- #
# Bridge relay with a CaptureSink (no live mesh)
# --------------------------------------------------------------------------- #
def test_bridge_publishes_one_attest_event_with_qseed_and_node():
    sink = qb.CaptureSink()
    bridge = qb.QosBridge(sink=sink, node="test-node", expected_qseed=QSEED)
    bridge.feed(QSEED_CAP.read_bytes())
    attests = [e for e in sink.events if e["subject"].endswith(".attest")]
    assert len(attests) == 1
    ev = attests[0]
    assert ev["subject"] == "KANNAKA.qos.test-node.attest"
    assert ev["payload"]["ok"] is True
    assert ev["payload"]["qseed"] == QSEED
    assert ev["payload"]["node"] == "test-node"


def test_bridge_publishes_attest_only_once_even_if_fed_twice():
    sink = qb.CaptureSink()
    bridge = qb.QosBridge(sink=sink, node="n", expected_qseed="none")
    blob = NOQSEED_CAP.read_bytes()
    bridge.feed(blob)
    bridge.feed(blob)  # re-feeding must not double-publish the attestation
    attests = [e for e in sink.events if e["subject"].endswith(".attest")]
    assert len(attests) == 1


def test_bridge_derives_node_from_qseed_when_unset():
    sink = qb.CaptureSink()
    bridge = qb.QosBridge(sink=sink, node=None, expected_qseed=QSEED)
    bridge.feed(QSEED_CAP.read_bytes())
    ev = [e for e in sink.events if e["subject"].endswith(".attest")][0]
    assert ev["subject"] == f"KANNAKA.qos.qos-{QSEED.lower()}.attest"


def test_bridge_relays_data_frames_to_data_subject():
    sink = qb.CaptureSink()
    bridge = qb.QosBridge(sink=sink, node="n", expected_qseed="none")
    # Prime with the real attestation, then feed a synthetic DATA/STATUS frame.
    bridge.feed(NOQSEED_CAP.read_bytes())
    data_frame = qb.encode_frame(qb.FRAME_DATA, bytes([qb.SWARM_OP_STATUS, 0x00, 0x00, 0x01, 0x00, 0x01]))
    bridge.feed(data_frame)
    datas = [e for e in sink.events if e["subject"].endswith(".data")]
    assert len(datas) == 1
    assert datas[0]["payload"]["op"] == "status"
    assert datas[0]["payload"]["opcode"] == qb.SWARM_OP_STATUS


def test_streaming_feed_matches_oneshot_verify():
    # Bit-for-bit: feeding the capture in small chunks yields the same verdict as
    # the one-shot verify.
    sink = qb.CaptureSink()
    bridge = qb.QosBridge(sink=sink, node="n", expected_qseed=QSEED)
    blob = QSEED_CAP.read_bytes()
    for i in range(0, len(blob), 37):
        bridge.feed(blob[i:i + 37])
    assert bridge.attest_result["ok"]
    assert bridge.attest_result["attested_qseed"] == QSEED


# --------------------------------------------------------------------------- #
# NATS sinks
# --------------------------------------------------------------------------- #
def test_kannaka_cli_sink_builds_inbox_argv_without_spawning():
    calls = {}

    class _FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_runner(argv, **kwargs):
        calls["argv"] = list(argv)
        return _FakeCompleted()

    sink = qb.KannakaCliSink(kannaka_bin="kannaka.exe", target="all", runner=fake_runner)
    sink.publish("KANNAKA.qos.n.attest", {"node": "n", "ok": True, "qseed": QSEED})
    argv = calls["argv"]
    assert argv[:5] == ["kannaka.exe", "inbox", "send", "all", "qos_attest"]
    assert "--arg" in argv
    # The intended qos subject + payload travel inside the JSON body.
    body = argv[-1]
    assert body.startswith("text=")
    parsed = json.loads(body[len("text="):])
    assert parsed["subject"] == "KANNAKA.qos.n.attest"
    assert parsed["qseed"] == QSEED


def test_kannaka_cli_sink_raises_on_nonzero_rc():
    class _Bad:
        returncode = 1
        stdout = ""
        stderr = "nats: connection refused"

    sink = qb.KannakaCliSink(runner=lambda argv, **kw: _Bad())
    with pytest.raises(RuntimeError, match="connection refused"):
        sink.publish("KANNAKA.qos.n.data", {"x": 1})


def test_null_sink_counts_but_discards():
    sink = qb.NullSink()
    sink.publish("KANNAKA.qos.n.attest", {"ok": True})
    sink.publish("KANNAKA.qos.n.data", {"op": "status"})
    assert sink.count == 2


# --------------------------------------------------------------------------- #
# End-to-end command entry points (file source, no NATS)
# --------------------------------------------------------------------------- #
def test_qos_bridge_verify_entrypoint_on_real_capture():
    res = qb.qos_bridge_verify(str(QSEED_CAP), expected_qseed=QSEED)
    assert res["ok"]
    assert res["path"] == str(QSEED_CAP)
    assert res["bytes"] == QSEED_CAP.stat().st_size


def test_qos_bridge_relay_once_file_no_nats_verifies_and_counts():
    out = qb.qos_bridge_relay(
        source="file", path=str(NOQSEED_CAP), node="n", expected_qseed="none",
        nats=False, once=True, timeout=5.0,
    )
    assert out["attestation"]["ok"]
    assert out["nats"] is False
    assert out["would_publish"] == 1
