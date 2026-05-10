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


def test_summary_stats_and_noise():
    from ansibench import report
    rows = [{"host": "h", "variant": "v", "param.n": 1, "wall_s": w, "maxrss_kb": 1024,
             "exit_code": 0, "timed_out": False} for w in (1.0, 1.0, 1.0)]
    rows += [{"host": "h", "variant": "v", "param.n": 2, "wall_s": w, "maxrss_kb": 2048,
              "exit_code": 0, "timed_out": False} for w in (1.0, 2.0, 3.0)]
    rows.append({"host": "h", "variant": "v", "param.n": 2, "wall_s": 99, "maxrss_kb": 0,
                 "exit_code": 1, "timed_out": False})
    a, b = report.summarize(rows)
    assert a["median_s"] == 1.0 and a["cv"] == 0 and a["failed"] == 0
    assert b["median_s"] == 2.0 and b["failed"] == 1  # the failed 99s run is left out
    assert b["cv"] == 0.5
    t = report.table([a, b])
    assert "n=2" in t and "noisy" in t and "1 failed (exit 1)" in t


def test_grab_metrics(tmp_path):
    f = tmp_path / "out"
    f.write_text("warming up gflops=1.0\nn=1024 gflops=812.5 time_ms=2.6\n")
    m = runner.grab_metrics(f, {"gflops": r"gflops=([0-9.]+)", "t": r"time_ms=([0-9.]+)",
                                "missing": r"nope=(\d+)", "pair": r"(n)=(\d+)"})
    assert m == {"gflops": 812.5, "t": 2.6, "missing": None, "pair": None}


def test_metrics_in_csv_and_summary(tmp_path):
    from ansibench import report
    d = tmp_path / "h1"
    d.mkdir()
    recs = [{"variant": "v", "params": {}, "rep": i, "wall_s": 1.0, "user_s": 1, "sys_s": 0,
             "maxrss_kb": 10, "exit_code": 0, "timed_out": False, "metrics": {"gflops": g}}
            for i, g in enumerate([10.0, 30.0, 20.0])]
    (d / "results.json").write_text(json.dumps(recs))
    report.write_csv(tmp_path)
    assert (tmp_path / "runs.csv").read_text().splitlines()[0].endswith("timed_out,metric.gflops")
    text = report.write_summary(tmp_path)
    assert "| gflops |" in text and "| 20 |" in text


PERF_OUT = """# started on Mon Aug 18 21:40:02 2025

1203944512,,cycles:u,401102233,100.00,,
2391022871,,instructions:u,401102233,100.00,1.99,insn per cycle
<not supported>,,cache-misses:u,0,100.00,,
"""


def test_parse_perf(tmp_path):
    f = tmp_path / "p"
    f.write_text(PERF_OUT)
    v = runner.parse_perf(f, ["cycles", "instructions", "cache-misses", "branch-misses"])
    assert v == {"cycles": 1203944512.0, "instructions": 2391022871.0,
                 "cache-misses": None, "branch-misses": None}


def test_perf_wrap_quotes():
    w = runner.perf_wrap("echo 'a b' | wc", ["cycles", "instructions"], "/tmp/x y")
    assert w.startswith("perf stat -x, -o '/tmp/x y' -e cycles,instructions -- bash -c ")


def test_ipc_column(tmp_path):
    from ansibench import report
    d = tmp_path / "h"
    d.mkdir()
    rec = {"variant": "v", "params": {}, "rep": 0, "wall_s": 1, "user_s": 1, "sys_s": 0, "maxrss_kb": 1,
           "exit_code": 0, "timed_out": False, "metrics": {}, "perf": {"cycles": 100.0, "instructions": 250.0}}
    (d / "results.json").write_text(json.dumps([rec]))
    (row,) = report.load_rows(tmp_path)
    assert row["perf.ipc"] == 2.5
    assert "| ipc |" in report.write_summary(tmp_path)


def test_env_expands_against_target_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = runner.job_env({"env": {"PATH": "/opt/cuda/bin:$PATH", "N": 3}})
    assert env["PATH"] == "/opt/cuda/bin:/usr/bin:/bin"
    assert env["N"] == "3"


def test_env_reaches_build_and_run(tmp_path):
    (tmp_path / "src").mkdir()
    job = {"env": {"GREETING": "hey"}, "builds": ["echo $GREETING > built"],
           "runs": [{"variant": "v", "cmd": "cat built; echo $GREETING"}]}
    runner.cmd_build(job, str(tmp_path))
    runner.cmd_bench(job, str(tmp_path))
    (rec,) = json.loads((tmp_path / "out" / "results.json").read_text())
    assert (tmp_path / "out" / rec["stdout"]).read_text() == "hey\nhey\n"


def test_pick_time(tmp_path):
    out = tmp_path / "o"
    out.write_text("kernel=x time_ms=2.5 gbps=100\n")
    rec = {"wall_s": 0.4}
    assert runner.pick_time(rec, out, None) == 0.4
    assert runner.pick_time(rec, out, {"from": "process"}) == 0.4
    assert runner.pick_time(rec, out, {"from": "output", "regex": r"time_ms=([0-9.]+)", "unit": "ms"}) == 0.0025
    assert runner.pick_time(rec, out, {"from": "output", "regex": r"nope=([0-9.]+)", "unit": "ms"}) is None
