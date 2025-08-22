"""Benchmark spec files (yaml).

    name: hello
    hosts: gpu            # optional, default pattern if --limit isn't given
    source:
      repo: https://github.com/me/thing
      ref: v1.2
    build: make
    run: ./bin/thing --fast
    repeat: 5
    warmup: 1
    timeout: 60

or, to compare a few versions of the same thing over some inputs:

    variants:
      naive: {build: make naive, run: ./naive {{ n }}}
      tiled: {build: make tiled, run: ./tiled {{ n }}}
    matrix:
      n: [512, 1024, 2048]
    metrics:
      gflops: 'gflops=([0-9.]+)'
    perf_stat: [cycles, instructions, cache-misses]

    machine:                 # everything here gets put back after the run
      cpu_governor: performance
      turbo: off
      smt: off
      pin_cores: [2, 3]
      drop_caches: true
      hugepages: 512
"""
import itertools
import re
from pathlib import Path

import yaml


class SpecError(Exception):
    pass


TOP_KEYS = {"name", "hosts", "source", "build", "run", "variants", "matrix", "metrics",
            "perf_stat", "machine", "repeat", "warmup", "timeout"}
MACHINE_KEYS = {"cpu_governor", "turbo", "smt", "pin_cores", "drop_caches", "hugepages"}
PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _check(cond, msg):
    if not cond:
        raise SpecError(msg)


def _as_list(x, what):
    if x is None:
        return []
    if isinstance(x, str):
        return [x]
    _check(isinstance(x, list) and all(isinstance(i, str) for i in x), f"{what} should be a string or list of strings")
    return x


def parse(data, base_dir="."):
    _check(isinstance(data, dict), "spec should be a mapping")
    unknown = set(data) - TOP_KEYS
    _check(not unknown, f"unknown keys: {', '.join(sorted(unknown))}")
    _check(isinstance(data.get("name"), str) and data["name"], "spec needs a name")

    source = data.get("source")
    if source is not None:
        _check(isinstance(source, dict), "source should be a mapping")
        _check(("repo" in source) != ("path" in source), "source needs exactly one of repo or path")
        _check(set(source) <= {"repo", "ref", "path"}, "source only takes repo, ref, path")
        if "path" in source:
            # relative paths are relative to the spec file, not cwd
            source = {"path": str((Path(base_dir) / source["path"]).resolve())}
        else:
            source = {"repo": source["repo"], "ref": str(source.get("ref", ""))}

    run = data.get("run")
    variants = data.get("variants")
    _check((run is None) != (variants is None), "spec needs either run or variants (not both)")
    if run is not None:
        _check(isinstance(run, str) and run.strip(), "run should be a command string")
        variants = {"default": {"run": run, "build": []}}
    else:
        _check(isinstance(variants, dict) and variants, "variants should be a mapping of name -> {run, build}")
        parsed = {}
        for vname, v in variants.items():
            if isinstance(v, str):
                v = {"run": v}
            _check(isinstance(v, dict) and set(v) <= {"run", "build"}, f"variant {vname}: takes run and build")
            _check(isinstance(v.get("run"), str) and v["run"].strip(), f"variant {vname} needs a run command")
            parsed[str(vname)] = {"run": v["run"], "build": _as_list(v.get("build"), f"variant {vname} build")}
        variants = parsed

    matrix = data.get("matrix") or {}
    _check(isinstance(matrix, dict), "matrix should be a mapping of param -> list")
    for k, vals in matrix.items():
        _check(isinstance(vals, list) and vals, f"matrix.{k} should be a non-empty list")
        _check(all(isinstance(x, (str, int, float, bool)) for x in vals), f"matrix.{k}: plain values only")

    for vname, v in variants.items():
        for name in PLACEHOLDER.findall(v["run"]):
            _check(name in matrix, f"variant {vname} uses {{{{ {name} }}}} but there's no matrix.{name}")
        for b in v["build"]:
            _check(not PLACEHOLDER.search(b), f"variant {vname}: builds can't use matrix params")

    metrics = data.get("metrics") or {}
    _check(isinstance(metrics, dict), "metrics should be a mapping of name -> regex")
    for k, rx in metrics.items():
        _check(isinstance(rx, str), f"metrics.{k} should be a regex string")
        try:
            c = re.compile(rx)
        except re.error as e:
            raise SpecError(f"metrics.{k}: bad regex: {e}")
        _check(c.groups >= 1, f"metrics.{k}: regex needs a (group) around the number")

    perf = _as_list(data.get("perf_stat"), "perf_stat")

    machine = data.get("machine") or {}
    _check(isinstance(machine, dict), "machine should be a mapping")
    unknown = set(machine) - MACHINE_KEYS
    _check(not unknown, f"machine: unknown keys: {', '.join(sorted(unknown))}")
    if "cpu_governor" in machine:
        _check(isinstance(machine["cpu_governor"], str), "machine.cpu_governor should be a string like performance")
    for k in ("turbo", "smt"):
        if k in machine:
            _check(machine[k] in (True, False, "on", "off"), f"machine.{k} should be on/off")
    if "drop_caches" in machine:
        _check(isinstance(machine["drop_caches"], bool), "machine.drop_caches should be true/false")
    if "hugepages" in machine:
        _check(isinstance(machine["hugepages"], int) and machine["hugepages"] >= 0, "machine.hugepages should be a count")
    if "pin_cores" in machine:
        pc = machine["pin_cores"]
        ok = (isinstance(pc, list) and pc and all(isinstance(c, int) and c >= 0 for c in pc)) or \
            (isinstance(pc, str) and re.fullmatch(r"\d+(,\d+)*", pc))
        _check(ok, "machine.pin_cores should be a list of cpu numbers, or \"2,3\"")

    for k in ("repeat", "warmup"):
        if k in data:
            _check(isinstance(data[k], int) and data[k] >= (1 if k == "repeat" else 0), f"{k} should be a whole number")
    if data.get("timeout") is not None:
        _check(isinstance(data["timeout"], (int, float)) and data["timeout"] > 0, "timeout should be seconds > 0")

    return {
        "name": data["name"],
        "hosts": data.get("hosts"),
        "source": source,
        "build": _as_list(data.get("build"), "build"),
        "variants": variants,
        "matrix": matrix,
        "metrics": metrics,
        "perf_stat": perf,
        "machine": machine,
        "repeat": data.get("repeat", 1),
        "warmup": data.get("warmup", 0),
        "timeout": data.get("timeout"),
    }


def load(path):
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise SpecError(f"no such spec: {path}")
    except yaml.YAMLError as e:
        raise SpecError(f"{path}: bad yaml: {e}")
    try:
        return parse(data, path.parent)
    except SpecError as e:
        raise SpecError(f"{path}: {e}")


def render(cmd, params):
    return PLACEHOLDER.sub(lambda m: str(params[m.group(1)]), cmd)


def combos(matrix):
    keys = list(matrix)
    for vals in itertools.product(*(matrix[k] for k in keys)):
        yield dict(zip(keys, vals))


def to_job(spec):
    builds = list(spec["build"])
    for v in spec["variants"].values():
        builds += [b for b in v["build"] if b not in builds]
    runs = [
        {"variant": vname, "params": params, "cmd": render(v["run"], params)}
        for vname, v in spec["variants"].items()
        for params in combos(spec["matrix"])
    ]
    return {
        "name": spec["name"],
        "builds": builds,
        "runs": runs,
        "metrics": spec["metrics"],
        "perf_stat": spec.get("perf_stat", []),
        "machine": spec.get("machine", {}),
        "repeat": spec["repeat"],
        "warmup": spec["warmup"],
        "timeout": spec["timeout"],
    }


def dump(spec):
    out = {"name": spec["name"]}
    if spec.get("hosts"):
        out["hosts"] = spec["hosts"]
    if spec.get("source"):
        out["source"] = {k: v for k, v in spec["source"].items() if v}
    if spec.get("build"):
        out["build"] = spec["build"][0] if len(spec["build"]) == 1 else spec["build"]
    vs = spec["variants"]
    if list(vs) == ["default"] and not vs["default"]["build"]:
        out["run"] = vs["default"]["run"]
    else:
        out["variants"] = {
            name: ({"build": v["build"][0] if len(v["build"]) == 1 else v["build"]} if v["build"] else {})
            | {"run": v["run"]}
            for name, v in vs.items()
        }
    if spec.get("matrix"):
        out["matrix"] = spec["matrix"]
    if spec.get("metrics"):
        out["metrics"] = spec["metrics"]
    if spec.get("perf_stat"):
        out["perf_stat"] = spec["perf_stat"]
    if spec.get("machine"):
        out["machine"] = spec["machine"]
    for k, default in (("repeat", 1), ("warmup", 0), ("timeout", None)):
        if spec.get(k) != default:
            out[k] = spec[k]
    return yaml.safe_dump(out, sort_keys=False)
