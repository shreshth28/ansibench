# runner.py runs locally too, so the timing bits get quick tests without the container
import json
import time

from ansibench import runner


def run_timed(tmp_path, cmd, timeout=None):
    return runner.timed(cmd, tmp_path, tmp_path / "o", tmp_path / "e", timeout)


def test_timed_basic(tmp_path):
    r = run_timed(tmp_path, "echo hi")
    assert r["exit_code"] == 0 and not r["timed_out"]
    assert (tmp_path / "o").read_text() == "hi\n"


def test_timeout_kills_whole_pipeline(tmp_path):
    t0 = time.time()
    r = run_timed(tmp_path, "sleep 30 | cat", timeout=0.5)
    assert time.time() - t0 < 5
    assert r["timed_out"]
    assert r["exit_code"] < 0  # killed by a signal


def test_bench_repeat_and_warmup(tmp_path):
    (tmp_path / "src").mkdir()
    job = {"runs": [{"variant": "a", "cmd": "echo run >> count"}], "repeat": 3, "warmup": 2}
    runner.cmd_bench(job, str(tmp_path))
    recs = json.loads((tmp_path / "out" / "results.json").read_text())
    assert [r["rep"] for r in recs] == [0, 1, 2]
    # warmups still actually ran
    assert (tmp_path / "src" / "count").read_text().count("run") == 5
