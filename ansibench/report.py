"""Reads the per-host results.json files and turns them into tables."""
import csv
import json
import statistics
from pathlib import Path

BASE_COLS = ["host", "variant", "rep", "wall_s", "user_s", "sys_s", "maxrss_kb", "exit_code", "timed_out"]


def load_rows(results_dir):
    rows = []
    for res in sorted(Path(results_dir).glob("*/results.json")):
        host = res.parent.name
        for r in json.loads(res.read_text()):
            row = {"host": host, **{k: v for k, v in r.items() if k not in ("params", "metrics")}}
            for k, v in r.get("params", {}).items():
                row[f"param.{k}"] = v
            for k, v in (r.get("metrics") or {}).items():
                row[f"metric.{k}"] = v
            for k, v in (r.get("perf") or {}).items():
                row[f"perf.{k}"] = v
            perf = r.get("perf") or {}
            if perf.get("cycles") and perf.get("instructions") is not None:
                row["perf.ipc"] = round(perf["instructions"] / perf["cycles"], 3)
            rows.append(row)
    return rows


def _keys(rows, prefix):
    seen = []
    for r in rows:
        seen += [k for k in r if k.startswith(prefix) and k not in seen]
    return seen


def columns(rows):
    return BASE_COLS[:2] + _keys(rows, "param.") + BASE_COLS[2:] + _keys(rows, "metric.") + _keys(rows, "perf.")


def write_csv(results_dir, rows=None):
    rows = load_rows(results_dir) if rows is None else rows
    path = Path(results_dir) / "runs.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns(rows), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


NOISY_CV = 0.05


def summarize(rows):
    """Group runs by host + variant + params and work out wall-time stats."""
    groups = {}
    for r in rows:
        params = tuple(sorted((k, r[k]) for k in r if k.startswith("param.")))
        groups.setdefault((r["host"], r["variant"], params), []).append(r)

    out = []
    for (host, variant, params), runs in groups.items():
        ok = [r for r in runs if r["exit_code"] == 0 and not r.get("timed_out")]
        walls = [r["wall_s"] for r in ok]
        s = {
            "host": host,
            "variant": variant,
            "params": {k[len("param."):]: v for k, v in params},
            "n": len(runs),
            "failed": len(runs) - len(ok),
            "timed_out": sum(1 for r in runs if r.get("timed_out")),
            "exit_codes": sorted({r["exit_code"] for r in runs if r["exit_code"] != 0 and not r.get("timed_out")}),
        }
        if walls:
            s["median_s"] = statistics.median(walls)
            s["min_s"] = min(walls)
            s["stdev_s"] = statistics.stdev(walls) if len(walls) > 1 else 0.0
            mean = statistics.fmean(walls)
            s["cv"] = s["stdev_s"] / mean if mean > 0 else 0.0
            s["maxrss_kb"] = max(r["maxrss_kb"] for r in ok)
        s["metrics"] = {}
        for k in _keys(runs, "metric."):
            vals = [r[k] for r in ok if r.get(k) is not None]
            s["metrics"][k[len("metric."):]] = statistics.median(vals) if vals else None
        ipcs = [r["perf.ipc"] for r in ok if r.get("perf.ipc") is not None]
        if ipcs:
            s["metrics"]["ipc"] = statistics.median(ipcs)
        out.append(s)
    return out


def _fmt_s(x):
    if x is None:
        return "-"
    return f"{x * 1000:.2f}ms" if x < 1 else f"{x:.3f}s"


def _fmt_num(x):
    if x is None:
        return "-"
    return f"{x:.4g}"


def table(summary):
    """Markdown table, reads fine in a terminal too."""
    has_params = any(s["params"] for s in summary)
    metric_names = []
    for s in summary:
        metric_names += [m for m in s.get("metrics", {}) if m not in metric_names]
    head = ["host", "variant"] + (["params"] if has_params else []) + \
        ["n", "median", "min", "stdev", "cv", "maxrss"] + metric_names + [""]
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    for s in summary:
        note = []
        if s.get("timed_out"):
            note.append(f"{s['timed_out']} timed out")
        if s["failed"] - s.get("timed_out", 0):
            codes = ",".join(map(str, s.get("exit_codes", [])))
            note.append(f"{s['failed'] - s.get('timed_out', 0)} failed (exit {codes})")
        if s.get("cv", 0) > NOISY_CV:
            note.append("noisy")
        cells = [s["host"], s["variant"]]
        if has_params:
            cells.append(" ".join(f"{k}={v}" for k, v in s["params"].items()) or "-")
        cells += [
            str(s["n"]),
            _fmt_s(s.get("median_s")),
            _fmt_s(s.get("min_s")),
            _fmt_s(s.get("stdev_s")),
            f"{s['cv'] * 100:.1f}%" if "cv" in s else "-",
            f"{s['maxrss_kb'] / 1024:.1f}MB" if "maxrss_kb" in s else "-",
            *(_fmt_num(s.get("metrics", {}).get(m)) for m in metric_names),
            ", ".join(note),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def machine_line(info):
    ls = info.get("lscpu", {})
    cpu = ls.get("Model name")
    if not cpu or cpu == "-":
        # arm boxes often don't have a model name, vendor + arch is better than "-"
        cpu = " ".join(x for x in (ls.get("Vendor ID"), info.get("arch")) if x)
    bits = [info.get("os"), cpu, f"{info.get('cpus')} cpus"]
    if info.get("mem_total_kb"):
        bits.append(f"{info['mem_total_kb'] / 1024 / 1024:.0f}GB")
    gov = (info.get("cpufreq") or {}).get("governor")
    bits.append(f"governor {gov or '-'}, turbo {info.get('turbo') or '-'}, smt {info.get('smt') or '-'}")
    for g in info.get("gpus", []):
        bits.append(f"{g.get('name')} (driver {g.get('driver_version')})")
    return ", ".join(b for b in bits if b)


def machines(results_dir):
    lines = []
    for f in sorted(Path(results_dir).glob("*/sysinfo.json")):
        lines.append(f"- {f.parent.name}: {machine_line(json.loads(f.read_text()))}")
    return "\n".join(lines)


def write_summary(results_dir, rows=None):
    rows = load_rows(results_dir) if rows is None else rows
    summary = summarize(rows)
    text = table(summary)
    noisy = [s for s in summary if s.get("cv", 0) > NOISY_CV]
    if noisy:
        text += (f"\n\nheads up: {len(noisy)} row(s) have >{NOISY_CV:.0%} variation between runs, "
                 "numbers might not mean much. more --repeat, or pin the machine down.")
    notes = []
    for f in sorted(Path(results_dir).glob("*/notes.json")):
        notes += [f"{f.parent.name}: {n}" for n in json.loads(f.read_text())]
    if notes:
        text += "\n\nnotes:\n" + "\n".join(f"- {n}" for n in notes)
    m = machines(results_dir)
    if m:
        text = f"{m}\n\n{text}"
    (Path(results_dir) / "summary.md").write_text(f"# {Path(results_dir).name}\n\n{text}\n")
    return text
