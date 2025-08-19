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
"""
from pathlib import Path

import yaml


class SpecError(Exception):
    pass


TOP_KEYS = {"name", "hosts", "source", "build", "run", "repeat", "warmup", "timeout"}


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
    _check(isinstance(run, str) and run.strip(), "spec needs a run command")

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
        "run": run,
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


def to_job(spec):
    return {
        "name": spec["name"],
        "builds": spec["build"],
        "runs": [{"variant": "default", "cmd": spec["run"]}],
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
    out["run"] = spec["run"]
    for k, default in (("repeat", 1), ("warmup", 0), ("timeout", None)):
        if spec.get(k) != default:
            out[k] = spec[k]
    return yaml.safe_dump(out, sort_keys=False)
