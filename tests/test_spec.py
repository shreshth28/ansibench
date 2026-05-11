import pytest
import yaml

from ansibench import spec
from ansibench.cli import main


def test_minimal():
    s = spec.parse({"name": "x", "run": "./a"})
    assert s["repeat"] == 1 and s["warmup"] == 0 and s["build"] == [] and s["source"] is None
    job = spec.to_job(s)
    assert job["runs"] == [{"variant": "default", "params": {}, "cmd": "./a"}]


def test_build_string_or_list():
    assert spec.parse({"name": "x", "run": "a", "build": "make"})["build"] == ["make"]
    assert spec.parse({"name": "x", "run": "a", "build": ["a", "b"]})["build"] == ["a", "b"]


def test_path_is_relative_to_spec(tmp_path):
    (tmp_path / "code").mkdir()
    f = tmp_path / "b.yml"
    f.write_text("name: x\nrun: a\nsource: {path: code}\n")
    assert spec.load(f)["source"]["path"] == str((tmp_path / "code").resolve())


@pytest.mark.parametrize("data,err", [
    ({"run": "a"}, "needs a name"),
    ({"name": "x"}, "either run or variants"),
    ({"name": "x", "run": "a", "variants": {"v": "b"}}, "either run or variants"),
    ({"name": "x", "variants": {"v": {"build": "make"}}}, "variant v needs a run"),
    ({"name": "x", "run": "./a {{ n }}"}, "no matrix.n"),
    ({"name": "x", "variants": {"v": {"run": "a", "build": "make N={{ n }}"}}, "matrix": {"n": [1]}}, "builds can't use"),
    ({"name": "x", "run": "a", "matrix": {"n": []}}, "non-empty"),
    ({"name": "x", "run": "a", "metrics": {"g": "gflops=[0-9.]+"}}, "needs a .group."),
    ({"name": "x", "run": "a", "metrics": {"g": "("}}, "bad regex"),
    ({"name": "x", "run": "a", "nope": 1}, "unknown keys: nope"),
    ({"name": "x", "run": "a", "repeat": 0}, "repeat"),
    ({"name": "x", "run": "a", "timeout": -1}, "timeout"),
    ({"name": "x", "run": "a", "source": {"repo": "r", "path": "p"}}, "exactly one"),
    ({"name": "x", "run": "a", "build": [1]}, "build"),
])
def test_validation(data, err):
    with pytest.raises(spec.SpecError, match=err):
        spec.parse(data)


def test_variants_times_matrix():
    s = spec.parse({
        "name": "mm",
        "build": "./configure",
        "variants": {
            "naive": {"build": "make naive", "run": "./naive {{ n }} {{t}}"},
            "tiled": {"build": ["make tiled"], "run": "./tiled {{ n }}"},
            "short": "./short",
        },
        "matrix": {"n": [1, 2], "t": ["a", "b"]},
    })
    job = spec.to_job(s)
    assert job["builds"] == ["./configure", "make naive", "make tiled"]
    assert len(job["runs"]) == 3 * 4
    assert job["runs"][0] == {"variant": "naive", "params": {"n": 1, "t": "a"}, "cmd": "./naive 1 a"}
    assert job["runs"][3]["cmd"] == "./naive 2 b"
    assert {r["cmd"] for r in job["runs"] if r["variant"] == "tiled"} == {"./tiled 1", "./tiled 2"}


def test_same_build_only_once():
    s = spec.parse({"name": "x", "variants": {"a": {"build": "make", "run": "a"}, "b": {"build": "make", "run": "b"}}})
    assert spec.to_job(s)["builds"] == ["make"]


def test_dump_roundtrip_variants():
    s = spec.parse({"name": "x", "variants": {"a": {"build": "make a", "run": "./a {{ n }}"}, "b": "./b"},
                    "matrix": {"n": [1, 2]}, "metrics": {"g": "g=([0-9.]+)"}})
    assert spec.parse(yaml.safe_load(spec.dump(s))) == s


def test_bad_yaml(tmp_path):
    f = tmp_path / "b.yml"
    f.write_text("name: [\n")
    with pytest.raises(spec.SpecError, match="bad yaml"):
        spec.load(f)


def test_unquoted_braces_hint(tmp_path):
    f = tmp_path / "b.yml"
    f.write_text("name: x\nvariants:\n  a: {run: ./a {{ n }}}\nmatrix: {n: [1]}\n")
    with pytest.raises(spec.SpecError, match="put quotes around"):
        spec.load(f)


def test_dump_roundtrip():
    s = spec.parse({"name": "x", "run": "a", "build": "make", "repeat": 3,
                    "source": {"repo": "r", "ref": "v1"}})
    assert spec.parse(yaml.safe_load(spec.dump(s))) == s


def test_run_needs_hosts(tmp_path, capsys):
    f = tmp_path / "b.yml"
    f.write_text("name: x\nrun: a\n")
    assert main(["run", str(f)]) == 2
    assert "no hosts" in capsys.readouterr().err


def test_exec_without_command(capsys):
    assert main(["exec", "somehost"]) == 2
    assert "nothing to run" in capsys.readouterr().err


def test_run_spec_on_target(target, tmp_path, capsys):
    (tmp_path / "code").mkdir()
    (tmp_path / "code" / "hello.sh").write_text("echo hello from spec\n")
    f = tmp_path / "b.yml"
    f.write_text("name: hello\nhosts: target\nsource: {path: code}\nrun: bash hello.sh\nrepeat: 2\n")
    res = tmp_path / "res"
    assert main(["run", str(f), "-i", str(target), "--results", str(res)]) == 0
    (d,) = list(res.iterdir())
    assert d.name.endswith("_hello")
    assert yaml.safe_load((d / "spec.yml").read_text())["repeat"] == 2
    assert "| target | default | 2 |" in capsys.readouterr().out


def test_exec_save_spec_then_run_it(target, tmp_path):
    f = tmp_path / "saved.yml"
    res = tmp_path / "res"
    assert main(["exec", "target", "-i", str(target), "--results", str(res), "-n", "2",
                 "--save-spec", str(f), "--", "echo", "hi there"]) == 0
    saved = yaml.safe_load(f.read_text())
    assert saved == {"name": "exec", "hosts": "target", "run": "echo 'hi there'", "repeat": 2}
    assert main(["run", str(f), "-i", str(target), "--results", str(res)]) == 0


def test_variants_matrix_on_target(target, tmp_path, capsys):
    f = tmp_path / "b.yml"
    f.write_text(
        "name: sweep\nhosts: target\n"
        "variants:\n  fast: 'sleep 0.0{{ n }}'\n  slow: 'sleep 0.{{ n }}'\n"
        "matrix: {n: [1, 2]}\n"
    )
    res = tmp_path / "res"
    assert main(["run", str(f), "-i", str(target), "--results", str(res)]) == 0
    (d,) = list(res.iterdir())
    csv_lines = (d / "runs.csv").read_text().splitlines()
    assert csv_lines[0].startswith("host,variant,param.n,rep,time_s,wall_s")
    assert len(csv_lines) == 1 + 4
    out = capsys.readouterr().out
    assert "| target | slow | n=2 | 1 |" in out


@pytest.mark.parametrize("machine,ok", [
    ({"cpu_governor": "performance", "turbo": "off", "smt": False, "pin_cores": [2, 3],
      "drop_caches": True, "hugepages": 64, "gpu": {"persistence_mode": "on", "lock_clocks_mhz": 1500}}, True),
    ({"pin_cores": "2,3"}, True),
    ({"pin_cores": "2-3"}, False),
    ({"turbo": "sometimes"}, False),
    ({"gpu": {"lock_clocks_mhz": "fast"}}, False),
    ({"gpu": {"overclock": 1}}, False),
    ({"fans": "max"}, False),
])
def test_machine_validation(machine, ok):
    data = {"name": "x", "run": "a", "machine": machine}
    if ok:
        s = spec.parse(data)
        assert spec.parse(yaml.safe_load(spec.dump(s))) == s
    else:
        with pytest.raises(spec.SpecError):
            spec.parse(data)


def test_env_in_spec():
    s = spec.parse({"name": "x", "run": "a", "env": {"PATH": "/usr/local/cuda/bin:$PATH", "OMP_NUM_THREADS": 4}})
    assert s["env"] == {"PATH": "/usr/local/cuda/bin:$PATH", "OMP_NUM_THREADS": "4"}
    assert spec.to_job(s)["env"] == s["env"]
    assert spec.parse(yaml.safe_load(spec.dump(s))) == s
    with pytest.raises(spec.SpecError, match="env"):
        spec.parse({"name": "x", "run": "a", "env": ["PATH=x"]})


def test_timing_default_is_process():
    s = spec.parse({"name": "x", "run": "a"})
    assert s["timing"] == {"from": "process"}
    assert "timing" not in yaml.safe_load(spec.dump(s))


def test_timing_from_output():
    t = {"from": "output", "regex": "time_ms=([0-9.]+)", "unit": "ms"}
    s = spec.parse({"name": "x", "run": "a", "timing": t})
    assert s["timing"] == t and spec.to_job(s)["timing"] == t
    assert spec.parse(yaml.safe_load(spec.dump(s))) == s


@pytest.mark.parametrize("t,err", [
    ({"from": "output"}, "regex is needed"),
    ({"from": "output", "regex": "t=[0-9]+"}, "needs a .group."),
    ({"from": "output", "regex": "t=([0-9]+)", "unit": "minutes"}, "unit"),
    ({"from": "gpu"}, "process or output"),
    ("wall", "timing should be"),
])
def test_timing_validation(t, err):
    with pytest.raises(spec.SpecError, match=err):
        spec.parse({"name": "x", "run": "a", "timing": t})
