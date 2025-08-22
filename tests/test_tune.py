import json
import os

import pytest

from ansibench import engine, runner
from tests.test_exec import on_target


def fake_sys(root, governor="schedutil", no_turbo="0", smt="on", hugepages="0"):
    cpu = root / "sys/devices/system/cpu"
    for i in range(4):
        d = cpu / f"cpu{i}/cpufreq"
        d.mkdir(parents=True)
        (d / "scaling_governor").write_text(governor)
        (d / "scaling_available_governors").write_text("performance powersave schedutil")
    (cpu / "intel_pstate").mkdir()
    (cpu / "intel_pstate/no_turbo").write_text(no_turbo)
    (cpu / "smt").mkdir()
    (cpu / "smt/control").write_text(smt)
    vm = root / "proc/sys/vm"
    vm.mkdir(parents=True)
    (vm / "nr_hugepages").write_text(hugepages)
    return root


@pytest.fixture
def sysroot(tmp_path, monkeypatch):
    root = fake_sys(tmp_path / "root")
    monkeypatch.setattr(runner, "SYSROOT", str(root))
    work = tmp_path / "work"
    (work / "out").mkdir(parents=True)
    return root, work


def snapshot(root):
    return {str(p.relative_to(root)): p.read_text() for p in root.rglob("*") if p.is_file()}


def notes(work):
    f = work / "out/notes.json"
    return json.loads(f.read_text()) if f.exists() else []


def test_tune_then_restore(sysroot):
    root, work = sysroot
    before = snapshot(root)
    job = {"machine": {"cpu_governor": "performance", "turbo": "off", "smt": "off", "hugepages": 64}}
    assert runner.cmd_tune(job, str(work)) == 0
    assert (root / "sys/devices/system/cpu/cpu3/cpufreq/scaling_governor").read_text() == "performance"
    assert (root / "sys/devices/system/cpu/intel_pstate/no_turbo").read_text() == "1"
    assert (root / "sys/devices/system/cpu/smt/control").read_text() == "off"
    assert (root / "proc/sys/vm/nr_hugepages").read_text() == "64"
    assert "tuned: smt=off, cpu_governor=performance, turbo=off, hugepages=64" in notes(work)

    assert runner.cmd_restore(job, str(work)) == 0
    assert snapshot(root) == before
    assert not (work / "tune_state.json").exists()


def test_boost_instead_of_intel_pstate(sysroot):
    root, work = sysroot
    (root / "sys/devices/system/cpu/intel_pstate/no_turbo").unlink()
    (root / "sys/devices/system/cpu/cpufreq").mkdir()
    (root / "sys/devices/system/cpu/cpufreq/boost").write_text("1")
    runner.cmd_tune({"machine": {"turbo": "off"}}, str(work))
    assert (root / "sys/devices/system/cpu/cpufreq/boost").read_text() == "0"
    runner.cmd_restore({}, str(work))
    assert (root / "sys/devices/system/cpu/cpufreq/boost").read_text() == "1"


def test_unknown_governor_is_skipped(sysroot):
    root, work = sysroot
    runner.cmd_tune({"machine": {"cpu_governor": "ludicrous"}}, str(work))
    assert (root / "sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read_text() == "schedutil"
    assert any("ludicrous not in" in n for n in notes(work))


def test_missing_knobs_get_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "SYSROOT", str(tmp_path))
    (tmp_path / "out").mkdir()
    runner.cmd_tune({"machine": {"cpu_governor": "performance", "turbo": "off", "smt": "off",
                                 "hugepages": 8}}, str(tmp_path))
    n = notes(tmp_path)
    for knob in ("cpu_governor", "turbo", "smt", "hugepages"):
        assert any(x.startswith(f"{knob}: skipped") for x in n), knob


def test_smt_forceoff_left_alone(sysroot):
    root, work = sysroot
    (root / "sys/devices/system/cpu/smt/control").write_text("forceoff")
    runner.cmd_tune({"machine": {"smt": "on"}}, str(work))
    assert (root / "sys/devices/system/cpu/smt/control").read_text() == "forceoff"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anything")
def test_readonly_file_is_noted_and_rest_still_restores(sysroot):
    root, work = sysroot
    (root / "proc/sys/vm/nr_hugepages").chmod(0o444)
    runner.cmd_tune({"machine": {"turbo": "off", "hugepages": 8}}, str(work))
    assert any("hugepages: couldn't write" in n for n in notes(work))
    runner.cmd_restore({}, str(work))
    assert (root / "sys/devices/system/cpu/intel_pstate/no_turbo").read_text() == "0"


# --- on the container, through ansible + sudo ---------------------------------

FAKE = "/tmp/fakesys"


@pytest.fixture
def remote_sysroot(target):
    on_target(
        f"sudo rm -rf {FAKE}; for i in 0 1; do sudo mkdir -p {FAKE}/sys/devices/system/cpu/cpu$i/cpufreq;"
        f" echo schedutil | sudo tee {FAKE}/sys/devices/system/cpu/cpu$i/cpufreq/scaling_governor >/dev/null; done;"
        f" sudo mkdir -p {FAKE}/sys/devices/system/cpu/intel_pstate;"
        f" echo 0 | sudo tee {FAKE}/sys/devices/system/cpu/intel_pstate/no_turbo >/dev/null"
    )
    return FAKE


# sysfs values get written without a trailing newline, so cat them one per line
READ_BACK = (f"for f in {FAKE}/sys/devices/system/cpu/cpu1/cpufreq/scaling_governor "
             f"{FAKE}/sys/devices/system/cpu/intel_pstate/no_turbo; do cat $f; echo; done")


def remote_state():
    return on_target(READ_BACK).split()


def job(**kw):
    j = {"name": "tune", "builds": [], "repeat": 1, "warmup": 0,
         "machine": {"cpu_governor": "performance", "turbo": "off"},
         # while the bench runs, read back what the knobs are set to
         "runs": [{"variant": "default", "params": {},
                   "cmd": READ_BACK}]}
    j.update(kw)
    return j


def test_tune_on_target_and_restore(target, remote_sysroot, tmp_path):
    res = engine.run(job(), target, "target", results_root=tmp_path,
                     target_env={"ANSIBENCH_SYSROOT": remote_sysroot})
    (r,) = json.loads((res / "target/results.json").read_text())
    assert (res / "target" / r["stdout"]).read_text().split() == ["performance", "1"]
    assert remote_state() == ["schedutil", "0"]
    assert "tuned: cpu_governor=performance, turbo=off" in (res / "summary.md").read_text()


def test_restore_runs_even_if_bench_blows_up(target, remote_sysroot, tmp_path):
    # repeat="x" makes the runner crash with a TypeError mid-bench
    with pytest.raises(engine.RunError):
        engine.run(job(repeat="x"), target, "target", results_root=tmp_path,
                   target_env={"ANSIBENCH_SYSROOT": remote_sysroot})
    assert remote_state() == ["schedutil", "0"]


def test_pinning_on_target(target, tmp_path):
    j = {"name": "pin", "builds": [], "repeat": 1, "warmup": 0, "machine": {"pin_cores": [1]},
         "runs": [{"variant": "default", "params": {}, "cmd": "grep Cpus_allowed_list /proc/self/status"}]}
    res = engine.run(j, target, "target", results_root=tmp_path)
    (r,) = json.loads((res / "target/results.json").read_text())
    assert (res / "target" / r["stdout"]).read_text().split()[-1] == "1"


def test_real_container_knobs_are_skipped_cleanly(target, tmp_path):
    j = {"name": "real", "builds": [], "repeat": 1, "warmup": 0,
         "machine": {"cpu_governor": "performance", "turbo": "off", "drop_caches": True},
         "runs": [{"variant": "default", "params": {}, "cmd": "true"}]}
    res = engine.run(j, target, "target", results_root=tmp_path)
    summary = (res / "summary.md").read_text()
    assert "cpu_governor: skipped" in summary and "turbo: skipped" in summary
    assert "drop_caches: skipped" in summary
