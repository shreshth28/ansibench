"""Reads the per-host results.json files and turns them into tables."""
import csv
import json
from pathlib import Path

BASE_COLS = ["host", "variant", "rep", "wall_s", "user_s", "sys_s", "maxrss_kb", "exit_code", "timed_out"]


def load_rows(results_dir):
    rows = []
    for res in sorted(Path(results_dir).glob("*/results.json")):
        host = res.parent.name
        for r in json.loads(res.read_text()):
            row = {"host": host, **{k: v for k, v in r.items() if k != "params"}}
            for k, v in r.get("params", {}).items():
                row[f"param.{k}"] = v
            rows.append(row)
    return rows


def columns(rows):
    params = sorted({k for r in rows for k in r if k.startswith("param.")})
    return BASE_COLS[:2] + params + BASE_COLS[2:]


def write_csv(results_dir, rows=None):
    rows = load_rows(results_dir) if rows is None else rows
    path = Path(results_dir) / "runs.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns(rows), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path
