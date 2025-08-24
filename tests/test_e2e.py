# end to end: a spec using most features, run on the container through every stage
import csv
import re

from ansibench.cli import main

PROG = r"""
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv)
{
	long n = atol(argv[1]), s = 0;
	for (long i = 0; i < n * 100000; i++)
		s += i % 7;
	printf("sum=%ld score=%.2f\n", s, n * 1.5);
	return 0;
}
"""

SPEC = """
name: e2e
hosts: target
source: {path: code}
build: echo prebuild > pre.txt
variants:
  O0: {build: gcc -O0 -o prog_O0 prog.c, run: './prog_O0 {{ n }}'}
  O2: {build: gcc -O2 -o prog_O2 prog.c, run: './prog_O2 {{ n }}'}
matrix:
  n: [1, 4]
metrics:
  score: 'score=([0-9.]+)'
repeat: 3
warmup: 1
timeout: 60
machine:
  cpu_governor: performance
  pin_cores: [0]
"""


def test_spec_end_to_end(target, tmp_path, capsys):
    (tmp_path / "code").mkdir()
    (tmp_path / "code" / "prog.c").write_text(PROG)
    f = tmp_path / "bench.yml"
    f.write_text(SPEC)

    res = tmp_path / "res"
    assert main(["run", str(f), "-i", str(target), "--results", str(res)]) == 0
    (d,) = list(res.iterdir())

    rows = list(csv.DictReader(open(d / "runs.csv")))
    assert len(rows) == 2 * 2 * 3
    assert all(r["exit_code"] == "0" for r in rows)
    assert {(r["variant"], r["param.n"], r["metric.score"]) for r in rows} == {
        ("O0", "1", "1.5"), ("O0", "4", "6.0"), ("O2", "1", "1.5"), ("O2", "4", "6.0")}

    out = capsys.readouterr().out
    assert re.search(r"\| target \| O2 \| n=4 \| 3 \| .* \| 6 \|", out)
    # container can't do the governor, but pinning works so there's no note about it
    notes = out.split("notes:")[1]
    assert "cpu_governor: skipped" in notes and "pin_cores" not in notes
