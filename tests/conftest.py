import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TARGET_INV = ROOT / "testenv" / "inventory.ini"


def _docker_ok():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def _ssh_up(port=2222, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1) as s:
                if s.recv(4).startswith(b"SSH"):
                    return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


@pytest.fixture(scope="session")
def target(monkeypatch_session):
    """Inventory path for the local ssh container. Starts it if needed,
    skips the test if docker isn't around."""
    if not _docker_ok():
        pytest.skip("docker not available")
    running = subprocess.run(
        ["docker", "ps", "-q", "-f", "name=^ansibench-target$"], capture_output=True, text=True
    ).stdout.strip()
    if not running:
        subprocess.run(["make", "-s", "test-target"], cwd=ROOT, check=True, capture_output=True)
    if not _ssh_up():
        pytest.fail("test target never came up")
    # inventory uses a relative key path
    monkeypatch_session.chdir(ROOT)
    return TARGET_INV


@pytest.fixture(scope="session")
def monkeypatch_session():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()
