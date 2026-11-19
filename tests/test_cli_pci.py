"""CLI with PCI discovery (--backend auto default, --list/--once, --no-pci)."""

import json
import sys

import pytest

from fake_nvml import make_pynvml
from fake_sysfs import FakeSysfs
from gputop.__main__ import main
from gputop.config import Config

IGPU, DGPU = "0000:00:02.0", "0000:01:00.0"


@pytest.fixture
def tree(tmp_path):
    t = FakeSysfs(tmp_path / "root")
    t.add_pci_ids({0x9A49: "TigerLake-LP GT2 [Iris Xe Graphics]"}, nvidia={0x1F9D: "TU117M"})
    return t


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.toml"  # isolate from the user's real config
    p.write_text("")
    return str(p)


def run(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_default_backend_is_auto():
    assert Config().backend == "auto"


def test_list_shows_every_gpu_with_status(tree, cfg, capsys):
    tree.add_card(0, pci=IGPU)
    tree.add_pci(DGPU, vendor="0x10de", device_id="0x1f9d", driver="nvidia",
                 runtime_status="suspended", power_state="D3cold")
    code, out, err = run(capsys, "--config", cfg, "--sysfs-root", str(tree.root), "--list")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].startswith("GPU 0: Intel TigerLake-LP GT2 [Iris Xe Graphics] · 0000:00:02.0 · ACTIVE (i915, D0)")
    assert lines[1] == "GPU 1: NVIDIA TU117M · 0000:01:00.0 · SUSPENDED (nvidia, D3cold)"
    assert "nvidia backend not used" in err and "not probed" in err


def test_once_json_fields(tree, cfg, capsys):
    tree.add_card(0, pci=IGPU)
    tree.add_pci(DGPU, driver="nouveau")
    code, out, _ = run(capsys, "--config", cfg, "--sysfs-root", str(tree.root), "--once")
    assert code == 0
    data = json.loads(out)
    igpu, dgpu = data["gpus"]
    for g in (igpu, dgpu):
        for key in ("status", "driver", "pci_address", "power_state", "vendor", "name", "live_stats_available"):
            assert key in g
    assert (igpu["status"], igpu["live_stats_available"], igpu["vendor"]) == ("ACTIVE", True, "Intel")
    assert igpu["sample"]["clock_graphics"] == 300
    assert (dgpu["status"], dgpu["driver"], dgpu["live_stats_available"]) == ("UNSUPPORTED", "nouveau", False)
    assert dgpu["pci_address"] == DGPU and dgpu["sample"] is None and dgpu["processes"] == []


def test_nvidia_active_via_fake_nvml(tree, cfg, capsys, monkeypatch):
    tree.add_pci(DGPU, vendor="0x10de", device_id="0x1f9d", driver="nvidia")
    monkeypatch.setitem(sys.modules, "pynvml", make_pynvml([{"bus": "00000000:01:00.0"}]))
    code, out, _ = run(capsys, "--config", cfg, "--sysfs-root", str(tree.root), "--once")
    (g,) = json.loads(out)["gpus"]
    assert g["status"] == "ACTIVE" and g["live_stats_available"] and g["sample"]["util_gpu"] == 87.0


def test_nothing_found_exits_1_with_reasons(tree, cfg, capsys, monkeypatch):
    import pynvml

    def boom(*a):
        raise pynvml.NVMLError(pynvml.NVML_ERROR_LIBRARY_NOT_FOUND)

    monkeypatch.setattr(pynvml, "nvmlInit", boom)
    monkeypatch.setattr(pynvml, "nvmlInitWithFlags", boom, raising=False)
    code, out, err = run(capsys, "--config", cfg, "--sysfs-root", str(tree.root), "--list")
    assert code == 1
    assert "no GPU found" in err
    for name in ("nvidia:", "intel:", "amd:"):
        assert name in err
    assert "--backend fake" in err


def test_no_pci_hides_pci_only_gpus(tree, cfg, capsys):
    tree.add_card(0, pci=IGPU)
    tree.add_pci(DGPU, driver=None)
    code, out, _ = run(capsys, "--config", cfg, "--sysfs-root", str(tree.root), "--list")
    assert len(out.splitlines()) == 2
    code, out, _ = run(capsys, "--config", cfg, "--sysfs-root", str(tree.root), "--no-pci", "--list")
    assert len(out.splitlines()) == 1 and "Intel" in out


def test_forced_backend_list_still_shows_pci_gpus(tree, cfg, capsys):
    tree.add_card(0, pci=IGPU)
    tree.add_pci(DGPU, driver=None, power_state="D3cold", enable=0)
    code, out, _ = run(capsys, "--config", cfg, "--sysfs-root", str(tree.root), "--backend", "intel", "--list")
    assert code == 0
    assert "ACTIVE" in out and "· OFF" in out


def test_bad_backend_spec(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--backend", "nvidia,bogus"])
    assert exc.value.code == 2
