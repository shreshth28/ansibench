"""Turns a job into an ansible-playbook run and unpacks what comes back."""
import json
import os
import re
import subprocess
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

from ansibench import report
from ansibench.inventory import InventoryError, ansible_bin

HERE = Path(__file__).resolve().parent


class RunError(Exception):
    pass


def slug(s):
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", s).strip("-") or "job"


def new_results_dir(root, name):
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    d = Path(root) / f"{stamp}_{slug(name)}"
    d.mkdir(parents=True)
    return d


def run(job, inventory_path, hosts, source=None, results_root="results", verbose=False, save=None,
        ask_become_pass=False, target_env=None):
    """job: dict that gets shipped as job.json (see runner.py)
    source: {"repo": url, "ref": ref} or {"path": dir} or None
    save: {filename: text} to drop into the results dir (e.g. the spec)
    target_env: extra env vars on the target (the tests use it to fake /sys)
    returns the local results dir"""
    source = source or {}
    results = new_results_dir(results_root, job["name"])
    for fname, text in (save or {}).items():
        (results / fname).write_text(text)

    with tempfile.TemporaryDirectory(prefix="ansibench-") as tmp:
        job_file = Path(tmp) / "job.json"
        job_file.write_text(json.dumps(job, indent=2))

        path = source.get("path")
        extra = {
            "ab_hosts": hosts,
            "ab_name": slug(job["name"]),
            "ab_repo": source.get("repo", ""),
            "ab_ref": source.get("ref", ""),
            "ab_path": str(Path(path).resolve()) if path else "",
            "ab_runner": str(HERE / "runner.py"),
            "ab_job": str(job_file),
            "ab_results": str(results.resolve()),
            # only need root if there's something to change at the machine level
            "ab_env": target_env or {},
            "ab_tune": any(k in (job.get("machine") or {}) for k in ("cpu_governor", "turbo", "smt", "hugepages")),
        }
        vars_file = Path(tmp) / "vars.json"
        vars_file.write_text(json.dumps(extra))

        try:
            playbook_bin = ansible_bin("ansible-playbook")
        except InventoryError as e:
            raise RunError(str(e))

        env = dict(os.environ, ANSIBLE_RETRY_FILES_ENABLED="False", ANSIBLE_NOCOLOR="1")
        proc = subprocess.run(
            [playbook_bin, "-i", str(inventory_path), str(HERE / "playbook.yml"), "-e", f"@{vars_file}"]
            + (["--ask-become-pass"] if ask_become_pass else []),
            # -K needs the terminal for the password prompt
            stdin=None if ask_become_pass else subprocess.DEVNULL,
            stdout=None if verbose else subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )

    (results / "ansible.log").write_text(proc.stdout or "")
    unpack(results)
    rows = report.load_rows(results)
    report.write_csv(results, rows)
    report.write_summary(results, rows)

    if proc.returncode != 0:
        raise RunError(f"ansible-playbook failed (exit {proc.returncode}), log: {results / 'ansible.log'}")
    return results


def unpack(results):
    for tgz in results.glob("*.tgz"):
        dest = results / tgz.name[: -len(".tgz")]
        dest.mkdir(exist_ok=True)
        with tarfile.open(tgz) as t:
            t.extractall(dest, filter="data")
        tgz.unlink()
