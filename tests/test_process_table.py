from gputop.backends.base import GPUProcess
from gputop.widgets.process_table import (
    MIN_COMMAND_W,
    SORT_KEYS,
    filter_processes,
    layout_columns,
    sort_processes,
)

PROCS = [
    GPUProcess(pid=30, gpu_index=0, vram_used=100, sm_util=None, type="Compute", user="nik", command="python a.py"),
    GPUProcess(pid=10, gpu_index=1, vram_used=None, sm_util=50, type="Graphics", user="root", command="Xorg"),
    GPUProcess(pid=20, gpu_index=0, vram_used=300, sm_util=5, type="C+G", user="alice", command="blender"),
]


def pids(procs):
    return [p.pid for p in procs]


def test_sort_by_vram_descending_with_none_last():
    assert pids(sort_processes(PROCS, "vram", False)) == [20, 30, 10]
    assert pids(sort_processes(PROCS, "vram", True)) == [10, 30, 20]


def test_sort_text_columns_ascending():
    assert pids(sort_processes(PROCS, "user", False)) == [20, 30, 10]
    assert pids(sort_processes(PROCS, "pid", False)) == [10, 20, 30]
    assert pids(sort_processes(PROCS, "sm", False)) == [10, 20, 30]


def test_unknown_sort_key_falls_back_to_vram():
    assert pids(sort_processes(PROCS, "bogus", False)) == [20, 30, 10]


def test_all_sort_keys_work():
    for key in SORT_KEYS:
        assert len(sort_processes(PROCS, key, False)) == 3


def test_filter_by_text_and_gpu():
    assert pids(filter_processes(PROCS, "PY")) == [30]
    assert pids(filter_processes(PROCS, "root")) == [10]
    assert pids(filter_processes(PROCS, "20")) == [20]
    assert pids(filter_processes(PROCS, "", gpu=0)) == [30, 20]
    assert pids(filter_processes(PROCS, "x", gpu=0)) == []


def test_layout_drops_columns_when_narrow():
    wide = [c.key for c, _ in layout_columns(120)]
    assert wide == ["pid", "user", "command", "gpu", "vram", "sm", "type"]
    narrow = layout_columns(40)
    keys = [c.key for c, _ in narrow]
    assert "pid" in keys and "command" in keys and "vram" in keys
    assert "type" not in keys
    cmd_w = dict((c.key, w) for c, w in narrow)["command"]
    assert cmd_w >= MIN_COMMAND_W or len(keys) == 3
    total = sum(w for _, w in narrow) + len(narrow) - 1
    assert total <= 40


def test_layout_hides_gpu_column_on_request():
    assert "gpu" not in [c.key for c, _ in layout_columns(120, {"gpu"})]


def test_vram_sort_falls_back_when_no_process_has_vram():
    from gputop.themes import Theme
    from gputop.widgets.process_table import ProcessTable

    no_vram = [GPUProcess(pid=p, gpu_index=0, user="u", command="c") for p in (30, 10, 20)]
    t = ProcessTable(Theme("default"), sort_key="vram")
    t.set_processes(no_vram)
    assert t.effective_sort_key == "pid"
    assert pids(t.rows) == [10, 20, 30]
    engines = [GPUProcess(pid=p, gpu_index=0, sm_util=sm, user="u", command="c") for p, sm in ((1, 5.0), (2, 50.0))]
    t.set_processes(engines)
    assert t.effective_sort_key == "sm" and pids(t.rows) == [2, 1]  # engine use when fdinfo has it
    t.set_processes(PROCS)  # VRAM data present again: the chosen sort applies
    assert t.effective_sort_key == "vram" and t.sort_key == "vram"
