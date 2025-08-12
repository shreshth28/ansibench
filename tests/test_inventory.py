import pytest

from ansibench import inventory
from ansibench.cli import main

INI = """
[gpu]
gpu-box ansible_host=10.0.0.12 ansible_user=me

[arm]
graviton ansible_host=1.2.3.4 ansible_user=ubuntu

[x86]
xeon

[cpu:children]
arm
x86
"""

YAML = """
all:
  children:
    gpu:
      hosts:
        gpu-box:
          ansible_host: 10.0.0.12
"""


@pytest.fixture
def ini(tmp_path):
    p = tmp_path / "inv.ini"
    p.write_text(INI)
    return p


def test_load_ini(ini):
    hosts = {h.name: h for h in inventory.load(ini)}
    assert set(hosts) == {"gpu-box", "graviton", "xeon"}
    assert hosts["gpu-box"].address == "10.0.0.12"
    assert hosts["gpu-box"].user == "me"
    # no ansible_host -> falls back to the name
    assert hosts["xeon"].address == "xeon"


def test_nested_groups(ini):
    hosts = {h.name: h for h in inventory.load(ini)}
    assert sorted(hosts["graviton"].groups) == ["arm", "cpu"]
    assert hosts["gpu-box"].groups == ["gpu"]


def test_load_yaml(tmp_path):
    p = tmp_path / "inv.yml"
    p.write_text(YAML)
    (h,) = inventory.load(p)
    assert h.name == "gpu-box" and h.address == "10.0.0.12"


def test_select(ini):
    hosts = inventory.load(ini)
    assert [h.name for h in inventory.select(hosts, "cpu")] == ["graviton", "xeon"]
    assert [h.name for h in inventory.select(hosts, "xeon")] == ["xeon"]
    assert len(inventory.select(hosts, None)) == 3
    with pytest.raises(inventory.InventoryError):
        inventory.select(hosts, "nope")


def test_missing_file(tmp_path):
    with pytest.raises(inventory.InventoryError):
        inventory.load(tmp_path / "nope.ini")


def test_hosts_cmd(ini, capsys):
    assert main(["hosts", "cpu", "-i", str(ini)]) == 0
    out = capsys.readouterr().out
    assert "graviton" in out and "ubuntu@1.2.3.4" in out
    assert "gpu-box" not in out


def test_hosts_cmd_bad_pattern(ini, capsys):
    assert main(["hosts", "nope", "-i", str(ini)]) == 2
    assert "no hosts match" in capsys.readouterr().err
