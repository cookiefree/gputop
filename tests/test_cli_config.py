import json

import pytest

from gputop.__main__ import main
from gputop.config import Config


def test_list_with_fake_backend(capsys):
    assert main(["--backend", "fake", "--fake-gpus", "3", "--list"]) == 0
    out = capsys.readouterr().out
    assert [line.split(":")[0] for line in out.splitlines()] == ["GPU 0", "GPU 1", "GPU 2"]
    assert "RTX 4090" in out


def test_once_outputs_json(capsys):
    assert main(["--backend", "fake", "--once", "--gpu", "1"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["backend"] == "fake"
    assert [g["info"]["index"] for g in data["gpus"]] == [1]
    sample = data["gpus"][0]["sample"]
    assert 0 <= sample["util_gpu"] <= 100
    assert isinstance(data["gpus"][0]["processes"], list)


def test_missing_gpu_index_is_an_error(capsys):
    assert main(["--backend", "fake", "--gpu", "5", "--list"]) == 1
    assert "no GPU with index 5" in capsys.readouterr().err


def test_amd_stub_reports_clearly(capsys):
    assert main(["--backend", "amd", "--list"]) == 1
    assert "not implemented" in capsys.readouterr().err


def test_bad_arguments_exit_2():
    with pytest.raises(SystemExit) as exc:
        main(["--interval", "50"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        main(["--gpu", "a,b"])


def test_nvidia_without_nvml_exits_1(capsys, monkeypatch):
    """Without a usable NVML (like on CI) we get a clear message and exit 1."""
    import pynvml

    def boom():
        raise pynvml.NVMLError(pynvml.NVML_ERROR_LIBRARY_NOT_FOUND)

    monkeypatch.setattr(pynvml, "nvmlInit", boom)
    monkeypatch.setattr(pynvml, "nvmlInitWithFlags", lambda *a: boom(), raising=False)
    # --no-pci: independent of the GPUs in the machine running the tests
    assert main(["--backend", "nvidia", "--no-pci", "--list"]) == 1
    err = capsys.readouterr().err
    assert "NVML" in err and "--backend fake" in err


def test_config_roundtrip(tmp_path):
    path = tmp_path / "config.toml"
    cfg = Config(interval_ms=250, theme="nord", sort_key="sm", sort_reverse=True, mouse=False)
    cfg.save(path)
    loaded = Config.load(path)
    assert loaded == cfg


def test_config_bad_values_fall_back(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('interval_ms = 1\nmouse = 3\nview = "weird"\ntheme = "gruvbox"\n')
    cfg = Config.load(path)
    assert cfg.interval_ms == 100  # clamped
    assert cfg.mouse is True  # wrong type ignored
    assert cfg.view == "all"
    assert cfg.theme == "gruvbox"


def test_config_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("this is [ not toml")
    cfg = Config.load(path)
    assert cfg == Config()
    assert getattr(cfg, "_load_error", None)


def test_save_changes_only_touches_changed_keys(tmp_path):
    path = tmp_path / "config.toml"
    Config(theme="nord", interval_ms=1000).save(path)
    Config().save_changes({"interval_ms": 250}, path)
    cfg = Config.load(path)
    assert cfg.theme == "nord" and cfg.interval_ms == 250
