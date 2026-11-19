"""PCI discovery: enumeration, names, status classification (fake sysfs)."""

import pytest

from fake_sysfs import FakeSysfs
from gputop import pcidiscovery as pci
from gputop.pcidiscovery import PciDiscovery, PciPower, classify, describe, normalize_address

IGPU, DGPU = "0000:00:02.0", "0000:01:00.0"


@pytest.fixture
def tree(tmp_path):
    return FakeSysfs(tmp_path)


def test_discovers_display_controllers_only(tree):
    tree.add_pci(IGPU, vendor="0x8086", device_id="0x0d26", driver="i915", boot_vga=1)
    tree.add_pci(DGPU, vendor="0x10de", device_id="0x0fe9", driver="nouveau", klass="0x030200")
    tree.add_pci("0000:02:00.0", vendor="0x1002", device_id="0x73bf", klass="0x038000", driver=None)
    tree.add_pci("0000:00:1f.3", vendor="0x8086", device_id="0xa348", klass="0x040300", driver="snd_hda_intel")
    tree.add_pci_ids({0x0D26: "Crystal Well Integrated Graphics Controller"})
    gpus = PciDiscovery(tree.root).discover()
    assert [g.address for g in gpus] == [IGPU, DGPU, "0000:02:00.0"]  # audio device skipped
    igpu, dgpu, amd = gpus
    assert igpu.vendor == "Intel" and igpu.name == "Intel Crystal Well Integrated Graphics Controller"
    assert igpu.boot_vga is True and igpu.ids == "8086:0d26"
    assert dgpu.name == "NVIDIA GK107M" and dgpu.class_id == 0x030200
    assert amd.vendor == "AMD" and amd.name == "AMD 73bf"  # not in pci.ids: vendor + device id


def test_name_fallback_without_pci_ids(tree):
    tree.add_pci(DGPU, vendor="0x10de", device_id="0x0fd2")
    (g,) = PciDiscovery(tree.root).discover()
    assert g.name == "NVIDIA 0fd2"


def test_no_pci_bus(tmp_path):
    assert PciDiscovery(tmp_path).discover() == []


def test_power_attributes(tree):
    tree.add_pci(DGPU, driver="nvidia", enable=1, runtime_status="suspended", power_state="D3cold")
    p = PciDiscovery(tree.root).power(DGPU)
    assert p == PciPower(driver="nvidia", enable=1, runtime_status="suspended", power_state="D3cold")
    assert not p.awake


def test_power_of_vanished_device(tree):
    assert PciDiscovery(tree.root).power("0000:09:00.0") is None


def test_switcheroo_is_read_if_available(tree):
    tree.add_pci(DGPU, driver="nouveau", power_state="D0")
    tree.set_switcheroo(["0:IGD:+:Pwr:0000:00:02.0", "1:DIS: :Off:0000:01:00.0"])
    p = PciDiscovery(tree.root).power(DGPU)
    assert p.switcheroo == "Off"
    assert classify(p, live=False) == pci.OFF


@pytest.mark.parametrize("power,live,status", [
    # driver bound and awake
    (PciPower("nvidia", 1, "active", "D0"), True, pci.ACTIVE),
    (PciPower("nvidia", 1, "active", "D0"), False, pci.UNSUPPORTED),
    (PciPower("nouveau", 1, "active", "D0"), False, pci.UNSUPPORTED),
    (PciPower("nvidia", 1, "unsupported", None), True, pci.ACTIVE),  # no runtime PM: always on
    # runtime-PM sleep
    (PciPower("nvidia", 1, "suspended", "D3cold"), True, pci.SUSPENDED),
    (PciPower("nvidia", 1, "suspended", None), False, pci.SUSPENDED),
    (PciPower("amdgpu", 1, "active", "D3hot"), False, pci.SUSPENDED),
    # off
    (PciPower(None, 0, "suspended", "D3cold"), False, pci.OFF),
    (PciPower("nouveau", 0, "active", "D3hot"), False, pci.OFF),  # a MacBook's switched-off dGPU
    (PciPower("nvidia", 1, "active", "D0", switcheroo="DynOff"), False, pci.OFF),
    # no driver
    (PciPower(None, 0, "active", "D0"), False, pci.NO_DRIVER),
    (PciPower(None, None, None, None), False, pci.NO_DRIVER),
])
def test_classify(power, live, status):
    assert classify(power, live) == status


def test_awake_means_safe_to_query():
    assert PciPower("nvidia", 1, "active", "D0").awake
    assert PciPower("nvidia", 1, "unsupported", None).awake
    assert not PciPower("nvidia", 1, "suspended", "D3cold").awake
    assert not PciPower("nvidia", 0, "active", "D0").awake
    assert not PciPower(None, 1, "active", "D0").awake
    assert not PciPower("nvidia", 1, "active", "D3hot").awake


def test_messages():
    assert describe(pci.OFF, PciPower(None, 0, None, "D3cold")) == "GPU is off - no data"
    assert describe(pci.SUSPENDED, PciPower("nvidia", 1, "suspended", "D3cold")) == \
        "suspended (D3cold) - not queried to avoid waking it"
    assert describe(pci.NO_DRIVER, PciPower(None, 0, "active", "D0")) == "no driver loaded"
    assert describe(pci.UNSUPPORTED, PciPower("nouveau", 1, "active", "D0")) == \
        "driver nouveau - no stats available"
    assert "NVML" in describe(pci.UNSUPPORTED, PciPower("nvidia", 1, "active", "D0"), "NVML missing")


@pytest.mark.parametrize("raw,norm", [
    ("00000000:01:00.0", "0000:01:00.0"),
    ("0000:0A:00.0", "0000:0a:00.0"),
    ("01:00.0", "0000:01:00.0"),
])
def test_normalize_address(raw, norm):
    assert normalize_address(raw) == norm


def test_discovery_is_read_only(tree):
    import os

    tree.add_pci(DGPU, driver="nvidia", runtime_status="suspended", power_state="D3cold")

    def snapshot():
        return {os.path.join(d, f): os.lstat(os.path.join(d, f)).st_mtime_ns
                for d, _, files in os.walk(tree.root) for f in files}

    before = snapshot()
    disc = PciDiscovery(tree.root)
    for g in disc.discover():
        disc.power(g.address)
    assert snapshot() == before
