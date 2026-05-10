"""Gets copied to the target and run there with the system python3.
Stdlib only, keep it that way.

    python3 runner.py sysinfo job.json
    python3 runner.py build job.json
    python3 runner.py tune job.json      (as root)
    python3 runner.py bench job.json
    python3 runner.py restore job.json   (as root)
"""
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time


def job_env(job):
    # $VARS in values expand against the target's env, so PATH: /usr/local/cuda/bin:$PATH works
    env = dict(os.environ)
    for k, v in (job.get("env") or {}).items():
        env[k] = os.path.expandvars(str(v))
    return env


def sh(cmd, cwd, log, env=None):
    with open(log, "w") as f:
        return subprocess.run(["bash", "-c", cmd], cwd=cwd, stdout=f, stderr=subprocess.STDOUT, env=env).returncode


def git_sha(src):
    p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def cmd_build(job, workdir):
    src = os.path.join(workdir, "src")
    out = os.path.join(workdir, "out")
    os.makedirs(out, exist_ok=True)

    builds = []
    for i, cmd in enumerate(job.get("builds", [])):
        log = os.path.join(out, f"build{i}.log")
        rc = sh(cmd, src, log, job_env(job))
        builds.append({"cmd": cmd, "exit_code": rc, "log": os.path.basename(log)})
        if rc != 0:
            break

    info = {"git_sha": git_sha(src), "builds": builds}
    with open(os.path.join(out, "build.json"), "w") as f:
        json.dump(info, f, indent=2)
    return 0 if all(b["exit_code"] == 0 for b in builds) else 1


def maxrss_kb(ru):
    # linux reports KB, macOS bytes
    return ru.ru_maxrss // 1024 if sys.platform == "darwin" else ru.ru_maxrss


def kill_group(p, fired):
    fired.set()
    try:
        # whole process group, otherwise `bash -c "a | b"` leaves b running
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def timed(cmd, cwd, stdout_path, stderr_path, timeout=None, env=None):
    fired = threading.Event()
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        t0 = time.perf_counter()
        p = subprocess.Popen(["bash", "-c", cmd], cwd=cwd, stdout=out, stderr=err, start_new_session=True, env=env)
        timer = threading.Timer(timeout, kill_group, (p, fired)) if timeout else None
        if timer:
            timer.start()
        # wait4 instead of p.wait() so we get rusage for just this child
        _, status, ru = os.wait4(p.pid, 0)
        wall = time.perf_counter() - t0
        if timer:
            timer.cancel()
    p.returncode = os.waitstatus_to_exitcode(status)
    return {
        "wall_s": round(wall, 6),
        "user_s": round(ru.ru_utime, 6),
        "sys_s": round(ru.ru_stime, 6),
        "maxrss_kb": maxrss_kb(ru),
        "exit_code": p.returncode,
        "timed_out": fired.is_set(),
    }


def grab_metrics(stdout_path, metrics):
    """last match of each regex in stdout -> float (None if it never showed up)"""
    if not metrics:
        return {}
    with open(stdout_path, errors="replace") as f:
        text = f.read()
    out = {}
    for name, rx in metrics.items():
        found = re.findall(rx, text)
        val = None
        if found:
            last = found[-1]
            last = last[0] if isinstance(last, tuple) else last
            try:
                val = float(last)
            except ValueError:
                pass
        out[name] = val
    return out


def perf_wrap(cmd, events, out_file):
    # perf's own overhead ends up in wall/user/sys too, it's small but it's there
    return f"perf stat -x, -o {shlex.quote(out_file)} -e {shlex.quote(','.join(events))} -- bash -c {shlex.quote(cmd)}"


def parse_perf(path, events):
    """perf stat -x, output: value,unit,event,... one per line, # lines are comments"""
    vals = {e: None for e in events}
    try:
        lines = open(path).read().splitlines()
    except FileNotFoundError:
        return vals
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        value, event = parts[0], parts[2]
        name = next((e for e in events if event == e or event.startswith(e + ":")), None)
        if name is None:
            continue
        try:
            vals[name] = float(value)
        except ValueError:
            vals[name] = None  # <not counted> / <not supported>
    return vals


def give_back(path):
    # tune/restore run under sudo, don't leave root-owned files for the bench step
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if os.geteuid() == 0 and uid and gid:
        os.chown(path, int(uid), int(gid))


def note(out, msg):
    path = os.path.join(out, "notes.json")
    notes = json.load(open(path)) if os.path.exists(path) else []
    if msg not in notes:
        notes.append(msg)
    with open(path, "w") as f:
        json.dump(notes, f, indent=2)
    give_back(path)


# --- machine tuning -------------------------------------------------------
# ANSIBENCH_SYSROOT lets the tests point this at a fake /sys + /proc tree
SYSROOT = os.environ.get("ANSIBENCH_SYSROOT", "/")


def sysp(path):
    return os.path.join(SYSROOT, path.lstrip("/"))


def write(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def set_files(paths, value, state, knob, out):
    """write value to every path, remembering what was there. all or nothing-ish:
    if one write fails we note it and stop, restore puts back whatever got changed"""
    for path in paths:
        old = read(path)
        try:
            write(path, value)
        except OSError as e:
            note(out, f"{knob}: couldn't write {path} ({e.strerror}), skipped")
            return False
        state["files"].append([path, old])
    return True


def tune_governor(want, state, out):
    paths = sorted(glob.glob(sysp("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor")))
    if not paths:
        return note(out, "cpu_governor: skipped, no cpufreq on this box")
    avail = (read(paths[0].replace("scaling_governor", "scaling_available_governors")) or "").split()
    if avail and want not in avail:
        return note(out, f"cpu_governor: skipped, {want} not in {' '.join(avail)}")
    if set_files(paths, want, state, "cpu_governor", out):
        state["applied"]["cpu_governor"] = want


def tune_turbo(want_on, state, out):
    no_turbo = sysp("/sys/devices/system/cpu/intel_pstate/no_turbo")
    boost = sysp("/sys/devices/system/cpu/cpufreq/boost")
    if os.path.exists(no_turbo):
        path, val = no_turbo, "0" if want_on else "1"
    elif os.path.exists(boost):
        path, val = boost, "1" if want_on else "0"
    else:
        return note(out, "turbo: skipped, no intel_pstate/no_turbo or cpufreq/boost")
    if set_files([path], val, state, "turbo", out):
        state["applied"]["turbo"] = "on" if want_on else "off"


def tune_smt(want_on, state, out):
    path = sysp("/sys/devices/system/cpu/smt/control")
    cur = read(path)
    if cur is None:
        return note(out, "smt: skipped, no smt/control")
    if cur in ("notsupported", "notimplemented", "forceoff"):
        return note(out, f"smt: skipped, smt/control says {cur}")
    if set_files([path], "on" if want_on else "off", state, "smt", out):
        state["applied"]["smt"] = "on" if want_on else "off"


def tune_hugepages(n, state, out):
    path = sysp("/proc/sys/vm/nr_hugepages")
    if read(path) is None:
        return note(out, "hugepages: skipped, no nr_hugepages")
    if not set_files([path], n, state, "hugepages", out):
        return
    got = read(path)
    if got != str(n):
        note(out, f"hugepages: asked for {n}, kernel gave {got} (not enough free contiguous memory?)")
    state["applied"]["hugepages"] = got


def nvsmi(*args):
    return subprocess.run(["nvidia-smi", *args], capture_output=True, text=True)


def tune_gpu(want, state, out):
    if not shutil.which("nvidia-smi"):
        return note(out, "gpu: skipped, no nvidia-smi")
    q = nvsmi("--query-gpu=index,persistence_mode", "--format=csv,noheader")
    if q.returncode != 0:
        return note(out, f"gpu: skipped, nvidia-smi failed: {(q.stderr or q.stdout).strip()}")
    gpus = [line.split(",")[0].strip() for line in q.stdout.strip().splitlines()]
    persist = {line.split(",")[0].strip(): line.split(",")[1].strip() for line in q.stdout.strip().splitlines()}
    if want.get("index") is not None:
        idx = str(want["index"])
        if idx not in gpus:
            return note(out, f"gpu: skipped, no gpu {idx} (have {', '.join(gpus)})")
        gpus = [idx]

    applied = []
    for i in gpus:
        if "persistence_mode" in want:
            on = on_off(want["persistence_mode"])
            r = nvsmi("-i", i, "-pm", "1" if on else "0")
            if r.returncode != 0:
                note(out, f"gpu {i}: persistence mode failed: {(r.stderr or r.stdout).strip()}")
            else:
                was = "1" if persist.get(i) == "Enabled" else "0"
                state["cmds"].append(["nvidia-smi", "-i", i, "-pm", was])
                applied.append(f"gpu{i}.persistence={'on' if on else 'off'}")
        if "lock_clocks_mhz" in want:
            mhz = str(want["lock_clocks_mhz"])
            r = nvsmi("-i", i, "-lgc", f"{mhz},{mhz}")
            if r.returncode != 0:
                note(out, f"gpu {i}: lock clocks failed (needs a datacenter/recent card + root): "
                          f"{(r.stderr or r.stdout).strip()}")
            else:
                state["cmds"].append(["nvidia-smi", "-i", i, "-rgc"])
                applied.append(f"gpu{i}.clocks={mhz}MHz")
    if applied:
        state["applied"]["gpu"] = " ".join(applied)


def on_off(v):
    return v in (True, "on", 1)


def cmd_tune(job, workdir):
    out = os.path.join(workdir, "out")
    m = job.get("machine") or {}
    state = {"files": [], "cmds": [], "applied": {}}
    # smt first so the governor gets set on the cpus that are left
    if "smt" in m:
        tune_smt(on_off(m["smt"]), state, out)
    if "cpu_governor" in m:
        tune_governor(m["cpu_governor"], state, out)
    if "turbo" in m:
        tune_turbo(on_off(m["turbo"]), state, out)
    if "hugepages" in m:
        tune_hugepages(m["hugepages"], state, out)
    if m.get("gpu"):
        tune_gpu(m["gpu"], state, out)
    with open(os.path.join(workdir, "tune_state.json"), "w") as f:
        json.dump(state, f, indent=2)
    give_back(os.path.join(workdir, "tune_state.json"))
    if state["applied"]:
        note(out, "tuned: " + ", ".join(f"{k}={v}" for k, v in state["applied"].items()))
    return 0


def cmd_restore(job, workdir):
    out = os.path.join(workdir, "out")
    path = os.path.join(workdir, "tune_state.json")
    if not os.path.exists(path):
        return 0
    state = json.load(open(path))
    failed = []
    for cmd in reversed(state.get("cmds", [])):
        if subprocess.run(cmd, capture_output=True).returncode != 0:
            failed.append(" ".join(cmd))
    for f, old in reversed(state["files"]):
        if old is None:
            continue
        try:
            write(f, old)
        except OSError:
            failed.append(f)
    if failed:
        note(out, "restore FAILED for: " + ", ".join(failed) + " -- check the box by hand")
        return 1
    os.remove(path)
    return 0


def drop_caches(out):
    try:
        os.sync()
        write(sysp("/proc/sys/vm/drop_caches"), "3")
        return
    except OSError:
        pass
    # not root, try passwordless sudo
    p = subprocess.run(["sudo", "-n", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"],
                       capture_output=True) if shutil.which("sudo") else None
    if not p or p.returncode != 0:
        note(out, "drop_caches: skipped, need root or passwordless sudo")
        return "skip"


def pin_prefix(cores, out):
    if not cores:
        return ""
    cores = [int(c) for c in (cores.split(",") if isinstance(cores, str) else cores)]
    if not shutil.which("taskset") or not hasattr(os, "sched_getaffinity"):
        note(out, "pin_cores: skipped, no taskset")
        return ""
    have = os.sched_getaffinity(0)
    missing = [c for c in cores if c not in have]
    if missing:
        note(out, f"pin_cores: skipped, cpu(s) {missing} not available here")
        return ""
    return f"taskset -c {','.join(map(str, cores))} "


UNITS = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9}


def pick_time(rec, stdout_path, timing):
    """seconds, either our own wall clock or whatever the program printed"""
    if not timing or timing.get("from", "process") == "process":
        return rec["wall_s"]
    v = grab_metrics(stdout_path, {"t": timing["regex"]})["t"]
    return None if v is None else v * UNITS[timing.get("unit", "s")]


def cmd_bench(job, workdir):
    src = os.path.join(workdir, "src")
    out = os.path.join(workdir, "out")
    raw = os.path.join(out, "raw")
    os.makedirs(raw, exist_ok=True)

    repeat = job.get("repeat", 1)
    warmup = job.get("warmup", 0)
    timeout = job.get("timeout")
    env = job_env(job)
    m = job.get("machine") or {}
    pin = pin_prefix(m.get("pin_cores"), out)
    want_drop = bool(m.get("drop_caches"))
    perf_events = job.get("perf_stat") or []
    if perf_events and not shutil.which("perf"):
        note(out, "perf_stat skipped: perf isn't installed on this host")
        perf_events = []

    records = []
    for i, r in enumerate(job.get("runs", [])):
        tag = f"{i:03d}_{r.get('variant', 'default')}"
        for w in range(warmup):
            # warmups aren't recorded, output kept around in case they blow up
            timed(r["cmd"], src, os.path.join(raw, f"{tag}.warmup{w}.stdout"),
                  os.path.join(raw, f"{tag}.warmup{w}.stderr"), timeout, env)
        for rep in range(repeat):
            stdout = os.path.join(raw, f"{tag}.rep{rep}.stdout")
            stderr = os.path.join(raw, f"{tag}.rep{rep}.stderr")
            rec = {"variant": r.get("variant", "default"), "params": r.get("params", {}),
                   "rep": rep, "cmd": r["cmd"]}
            cmd = r["cmd"]
            if pin:
                cmd = pin + "bash -c " + shlex.quote(cmd)
            if want_drop and drop_caches(out) == "skip":
                want_drop = False
            perf_out = os.path.join(raw, f"{tag}.rep{rep}.perf")
            if perf_events:
                cmd = perf_wrap(cmd, perf_events, perf_out)
            rec.update(timed(cmd, src, stdout, stderr, timeout, env))
            rec["metrics"] = grab_metrics(stdout, job.get("metrics"))
            rec["timing"] = (job.get("timing") or {}).get("from", "process")
            rec["time_s"] = pick_time(rec, stdout, job.get("timing"))
            if perf_events:
                rec["perf"] = parse_perf(perf_out, perf_events)
            rec["stdout"] = os.path.relpath(stdout, out)
            rec["stderr"] = os.path.relpath(stderr, out)
            records.append(rec)

    with open(os.path.join(out, "results.json"), "w") as f:
        json.dump(records, f, indent=2)
    return 0


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def first_line(cmd):
    if not shutil.which(cmd[0]):
        return None
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (p.stdout or p.stderr).strip().splitlines()
    return out[0] if out else None


def lscpu():
    if not shutil.which("lscpu"):
        return {}
    p = subprocess.run(["lscpu"], capture_output=True, text=True, env=dict(os.environ, LC_ALL="C"))
    info = {}
    for line in p.stdout.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    return info


def os_release():
    for line in (read("/etc/os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip('"')
    return None


def turbo_state():
    no_turbo = read("/sys/devices/system/cpu/intel_pstate/no_turbo")
    if no_turbo is not None:
        return "off" if no_turbo == "1" else "on"
    boost = read("/sys/devices/system/cpu/cpufreq/boost")
    if boost is not None:
        return "on" if boost == "1" else "off"
    return None


def meminfo_kb(key):
    for line in (read("/proc/meminfo") or "").splitlines():
        if line.startswith(key + ":"):
            return int(line.split()[1])
    return None


def gpus():
    if not shutil.which("nvidia-smi"):
        return []
    fields = "name,driver_version,persistence_mode,clocks.sm,clocks.max.sm,power.limit"
    p = subprocess.run(["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return []
    return [dict(zip(fields.split(","), (x.strip() for x in line.split(","))))
            for line in p.stdout.strip().splitlines()]


def cmd_sysinfo(job, workdir):
    out = os.path.join(workdir, "out")
    os.makedirs(out, exist_ok=True)
    cpu = "/sys/devices/system/cpu"
    info = {
        "hostname": os.uname().nodename,
        "kernel": " ".join(os.uname()),
        "os": os_release(),
        "arch": os.uname().machine,
        "cpus": os.cpu_count(),
        "mem_total_kb": meminfo_kb("MemTotal"),
        "hugepages": read("/proc/sys/vm/nr_hugepages"),
        "cmdline": read("/proc/cmdline"),
        "cpufreq": {
            "driver": read(f"{cpu}/cpu0/cpufreq/scaling_driver"),
            "governor": read(f"{cpu}/cpu0/cpufreq/scaling_governor"),
            "min_khz": read(f"{cpu}/cpu0/cpufreq/scaling_min_freq"),
            "max_khz": read(f"{cpu}/cpu0/cpufreq/scaling_max_freq"),
        },
        "turbo": turbo_state(),
        "smt": read(f"{cpu}/smt/control"),
        "lscpu": lscpu(),
        "tools": {
            name: first_line(cmd) for name, cmd in {
                "gcc": ["gcc", "--version"],
                "clang": ["clang", "--version"],
                "nvcc": ["nvcc", "--version"],
                "python3": ["python3", "--version"],
                "perf": ["perf", "--version"],
            }.items()
        },
        "gpus": gpus(),
    }
    with open(os.path.join(out, "sysinfo.json"), "w") as f:
        json.dump(info, f, indent=2)
    return 0


def main(argv):
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    action, job_path = argv[1], argv[2]
    with open(job_path) as f:
        job = json.load(f)
    workdir = os.path.dirname(os.path.abspath(job_path))
    if action == "sysinfo":
        return cmd_sysinfo(job, workdir)
    if action == "build":
        return cmd_build(job, workdir)
    if action == "tune":
        return cmd_tune(job, workdir)
    if action == "restore":
        return cmd_restore(job, workdir)
    if action == "bench":
        return cmd_bench(job, workdir)
    print(f"unknown action {action}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
