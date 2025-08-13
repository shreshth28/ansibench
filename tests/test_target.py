import subprocess
import sys
from pathlib import Path

from ansibench import inventory


def test_target_inventory(target):
    (h,) = inventory.load(target)
    assert h.name == "target" and h.address == "127.0.0.1"


def test_ansible_can_reach_target(target):
    ansible = Path(sys.executable).parent / "ansible"
    proc = subprocess.run(
        [str(ansible), "all", "-i", str(target), "-m", "ping"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert '"ping": "pong"' in proc.stdout
