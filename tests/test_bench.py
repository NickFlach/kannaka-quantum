"""Network-free tests for the recall-correspondence benchmark harness (T2.2).

The aggregation, regression gate, and CLI wiring are exercised with a mocked
recall backend (no circuit). Two tests run the *real* local state-vector backend
on a tiny corpus and on the committed corpus — both hermetic ($0, no qBraid),
which is exactly what CI uses, so the committed corpus/baseline can't drift from
the code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kannaka_quantum import bench, core
from kannaka_quantum.cli import main

REPO = Path(__file__).resolve().parents[1]
CORPUS = REPO / "bench" / "corpus.json"
BASELINE = REPO / "bench" / "baseline.json"


# --- corpus loading --------------------------------------------------------- #
def _corpus(scenarios: list[dict]) -> dict:
    return {"format": bench.CORPUS_FORMAT, "generated_at": "t", "n": len(scenarios), "scenarios": scenarios}


def _scn(amps: list[float], hemisphere: str = "flat") -> dict:
    argmax = max(range(len(amps)), key=lambda i: amps[i]) if amps else None
    labels = [f"lbl{i:02d}" for i in range(len(amps))]
    return {
        "query_label": labels[argmax] if argmax is not None else None,
        "candidates": [{"label": labels[i], "amplitude": a} for i, a in enumerate(amps)],
        "classical_argmax": argmax,
        "hemisphere": hemisphere,
        "timestamp": "2026-07-01T00:00:00+00:00",
    }


def test_load_corpus_ok(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(_corpus([_scn([0.9, 0.1])])), encoding="utf-8")
    data = bench.load_corpus(p)
    assert data["format"] == bench.CORPUS_FORMAT
    assert len(data["scenarios"]) == 1


def test_load_corpus_rejects_wrong_format(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"format": "something/9", "scenarios": []}), encoding="utf-8")
    with pytest.raises(bench.BenchError, match="unsupported corpus format"):
        bench.load_corpus(p)


def test_load_corpus_missing_file(tmp_path):
    with pytest.raises(bench.BenchError, match="not found"):
        bench.load_corpus(tmp_path / "nope.json")


def test_load_corpus_requires_scenarios(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"format": bench.CORPUS_FORMAT}), encoding="utf-8")
    with pytest.raises(bench.BenchError, match="no 'scenarios'"):
        bench.load_corpus(p)


# --- aggregation (mocked backend) ------------------------------------------- #
def _mk_recall(disagree_indices: set[int] | None = None):
    """A quantum_recall stand-in. Agrees (quantum_top == classical_top) except on
    scenarios whose 0-based scored index is in ``disagree_indices``."""
    disagree = disagree_indices or set()
    state = {"n": 0}

    def _recall(amplitudes, labels=None, shots=1024, amplify=True, device=None):
        idx = state["n"]
        state["n"] += 1
        classical = int(max(range(len(amplitudes)), key=lambda i: amplitudes[i]))
        quantum = 0 if (idx in disagree and classical != 0) else classical

        def lbl(i):
            return labels[i] if labels is not None else i

        return {
            "quantum_top": lbl(quantum),
            "classical_top": lbl(classical),
            "agree": quantum == classical,
            "candidates": len(amplitudes),
            "qubits": 1,
            "iterations": 0,
            "amplified": False,
            "distribution": {},
        }

    return _recall


def test_run_bench_all_agree():
    corpus = _corpus([_scn([0.9, 0.1]), _scn([0.2, 0.8, 0.1]), _scn([0.5, 0.4])])
    res = bench.run_bench(corpus, recall_fn=_mk_recall())
    assert res["scenarios_scored"] == 3
    assert res["agreements"] == 3
    assert res["agreement_rate"] == 100.0
    assert res["argmax_mismatches"] == 0
    assert res["format"] == bench.RESULT_FORMAT


def test_run_bench_partial_agreement_rate():
    # 4 scenarios, 1 disagreement → 75%.
    corpus = _corpus([_scn([0.1, 0.9]), _scn([0.9, 0.1]), _scn([0.2, 0.7, 0.1]), _scn([0.3, 0.6])])
    res = bench.run_bench(corpus, recall_fn=_mk_recall(disagree_indices={2}))
    assert res["scenarios_scored"] == 4
    assert res["agreements"] == 3
    assert res["agreement_rate"] == 75.0


def test_run_bench_skips_empty_candidate_scenarios():
    corpus = _corpus([_scn([0.9, 0.1]), _scn([])])
    res = bench.run_bench(corpus, recall_fn=_mk_recall())
    assert res["scenarios_total"] == 2
    assert res["scenarios_scored"] == 1
    assert res["scenarios_skipped"] == 1
    assert res["agreement_rate"] == 100.0


def test_run_bench_flags_argmax_mismatch():
    # Corpus claims argmax index 0, but amplitudes make index 1 the real argmax.
    scn = _scn([0.9, 0.1])
    scn["classical_argmax"] = 1  # wrong on purpose
    res = bench.run_bench(_corpus([scn]), recall_fn=_mk_recall())
    assert res["argmax_mismatches"] == 1


# --- regression gate -------------------------------------------------------- #
def test_evaluate_regression_within_threshold_passes():
    reg = bench.evaluate_regression(98.5, 100.0, threshold=2.0)
    assert reg["drop"] == 1.5
    assert reg["regressed"] is False


def test_evaluate_regression_beyond_threshold_fails():
    reg = bench.evaluate_regression(95.0, 100.0, threshold=2.0)
    assert reg["drop"] == 5.0
    assert reg["regressed"] is True


def test_evaluate_regression_improvement_passes():
    reg = bench.evaluate_regression(100.0, 96.0, threshold=2.0)
    assert reg["drop"] == -4.0
    assert reg["regressed"] is False


# --- bench_command orchestration -------------------------------------------- #
def _write_corpus(tmp_path, scenarios) -> str:
    p = tmp_path / "corpus.json"
    p.write_text(json.dumps(_corpus(scenarios)), encoding="utf-8")
    return str(p)


def test_bench_command_update_baseline_writes_and_passes(tmp_path):
    corpus = _write_corpus(tmp_path, [_scn([0.9, 0.1]), _scn([0.2, 0.8])])
    baseline = tmp_path / "baseline.json"
    result, code = bench.bench_command(
        scenarios=corpus, baseline=str(baseline), update_baseline=True, recall_fn=_mk_recall()
    )
    assert code == 0
    assert baseline.exists()
    saved = json.loads(baseline.read_text())
    assert saved["format"] == bench.BASELINE_FORMAT
    assert saved["agreement_rate"] == 100.0


def test_bench_command_update_baseline_requires_path(tmp_path):
    corpus = _write_corpus(tmp_path, [_scn([0.9, 0.1])])
    with pytest.raises(bench.BenchError, match="requires --baseline"):
        bench.bench_command(scenarios=corpus, update_baseline=True, recall_fn=_mk_recall())


def test_bench_command_regression_returns_exit_1(tmp_path):
    corpus = _write_corpus(tmp_path, [_scn([0.1, 0.9]), _scn([0.9, 0.1]), _scn([0.2, 0.7]), _scn([0.3, 0.6])])
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"format": bench.BASELINE_FORMAT, "agreement_rate": 100.0}), encoding="utf-8")
    # One disagreement → 75% → 25-point drop → regressed.
    result, code = bench.bench_command(
        scenarios=corpus, baseline=str(baseline), recall_fn=_mk_recall(disagree_indices={0})
    )
    assert code == 1
    assert result["regression"]["regressed"] is True


def test_bench_command_clean_run_returns_exit_0(tmp_path):
    corpus = _write_corpus(tmp_path, [_scn([0.9, 0.1]), _scn([0.2, 0.8])])
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"format": bench.BASELINE_FORMAT, "agreement_rate": 100.0}), encoding="utf-8")
    result, code = bench.bench_command(scenarios=corpus, baseline=str(baseline), recall_fn=_mk_recall())
    assert code == 0
    assert result["regression"]["regressed"] is False


def test_bench_command_writes_out(tmp_path):
    corpus = _write_corpus(tmp_path, [_scn([0.9, 0.1])])
    out = tmp_path / "sub" / "result.json"
    _result, code = bench.bench_command(scenarios=corpus, out=str(out), recall_fn=_mk_recall())
    assert code == 0
    assert out.exists()
    assert json.loads(out.read_text())["agreement_rate"] == 100.0


# --- real local backend (hermetic, $0) -------------------------------------- #
def test_local_backend_recall_agrees():
    corpus = _corpus([_scn([0.9, 0.3, 0.2, 0.1]), _scn([0.30, 0.28, 0.26, 0.25]), _scn([0.7, 0.1])])
    res = bench.run_bench(corpus, device=core.LOCAL_DEVICE, shots=2048)
    assert res["scenarios_scored"] == 3
    assert res["agreement_rate"] == 100.0
    assert res["device"] == core.LOCAL_DEVICE


def test_cli_bench_runs_and_prints_json(tmp_path, capsys):
    corpus = _write_corpus(tmp_path, [_scn([0.9, 0.1]), _scn([0.2, 0.7, 0.1])])
    code = main(["bench", "--scenarios", corpus, "--device", core.LOCAL_DEVICE, "--shots", "512"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["format"] == bench.RESULT_FORMAT
    assert out["agreement_rate"] == 100.0


def test_cli_bench_regression_exit_1(tmp_path, capsys):
    corpus = _write_corpus(tmp_path, [_scn([0.9, 0.1])])
    baseline = tmp_path / "baseline.json"
    # Impossible baseline (>100%) forces a regression regardless of the run.
    baseline.write_text(json.dumps({"format": bench.BASELINE_FORMAT, "agreement_rate": 200.0}), encoding="utf-8")
    code = main(
        ["bench", "--scenarios", corpus, "--device", core.LOCAL_DEVICE, "--baseline", str(baseline)]
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out)["regression"]["regressed"] is True


# --- committed artifacts stay consistent ------------------------------------ #
def test_committed_corpus_meets_committed_baseline():
    """The shipped corpus, run on the local state-vector, must not regress past
    the shipped baseline — locks corpus + baseline + code together in CI."""
    assert CORPUS.exists(), "bench/corpus.json is committed"
    assert BASELINE.exists(), "bench/baseline.json is committed"
    result, code = bench.bench_command(
        scenarios=str(CORPUS), device=core.LOCAL_DEVICE, shots=2048, baseline=str(BASELINE)
    )
    assert code == 0, f"committed corpus regressed: {result.get('regression')}"
    assert result["scenarios_skipped"] == 0
    assert result["argmax_mismatches"] == 0
