import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


class InventoryError(Exception):
    pass


@dataclass
class Host:
    name: str
    groups: list[str] = field(default_factory=list)
    vars: dict = field(default_factory=dict)

    @property
    def address(self):
        return self.vars.get("ansible_host", self.name)

    @property
    def user(self):
        return self.vars.get("ansible_user")


def _ansible_bin(name):
    # prefer the one in our venv so we don't need ansible on PATH
    local = Path(sys.executable).parent / name
    if local.exists():
        return str(local)
    found = shutil.which(name)
    if not found:
        raise InventoryError(f"{name} not found, is ansible-core installed?")
    return found


def load(path):
    """Parse an inventory (ini or yaml) by asking ansible-inventory for json,
    so we get the exact same view of hosts/vars that ansible-playbook will."""
    path = Path(path)
    if not path.exists():
        raise InventoryError(f"inventory not found: {path}")

    env = dict(os.environ, ANSIBLE_INVENTORY_UNPARSED_WARNING="False")
    proc = subprocess.run(
        [_ansible_bin("ansible-inventory"), "-i", str(path), "--list"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        raise InventoryError(proc.stderr.strip() or "ansible-inventory failed")

    return _parse(json.loads(proc.stdout))


def _parse(data):
    hostvars = data.get("_meta", {}).get("hostvars", {})
    hosts = {h: Host(h, vars=hostvars.get(h, {})) for h in _hosts_under(data, "all", set())}

    # a host belongs to every group that contains it, directly or via :children
    for g in sorted(data):
        if g in ("_meta", "all", "ungrouped"):
            continue
        for h in _hosts_under(data, g, set()):
            hosts[h].groups.append(g)

    return sorted(hosts.values(), key=lambda h: h.name)


def _hosts_under(data, group, seen):
    if group in seen:
        return set()
    seen.add(group)
    node = data.get(group, {})
    out = set(node.get("hosts", []))
    for child in node.get("children", []):
        out |= _hosts_under(data, child, seen)
    return out


def select(hosts, pattern):
    """Pick hosts by name or group. 'all' or None means everything."""
    if not pattern or pattern == "all":
        return hosts
    picked = [h for h in hosts if h.name == pattern or pattern in h.groups]
    if not picked:
        raise InventoryError(f"no hosts match '{pattern}'")
    return picked
