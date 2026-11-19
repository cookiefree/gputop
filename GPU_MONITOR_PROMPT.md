# Project: `gputop` — a btop-style multi-GPU terminal monitor for Linux

## Goal

Build a terminal GPU monitor for Linux that looks and feels like **btop**, but is focused entirely on GPUs. It must handle **multiple GPUs at once** and show activity, memory, power draw, temperature, clocks and per-process usage for each one, live.

Think "btop, but the whole screen is GPU stats."

## Design requirements (btop look and feel)

- Rounded box-drawing borders with a title embedded in the top border of each panel (e.g. `┌─ GPU 0 · RTX 4070 ─┐` in btop's style).
- Braille-character graphs (⣀⣄⣤⣦⣶⣷⣿) for rolling history, like btop's CPU graph. Gradient coloring from green -> yellow -> red depending on value.
- Meters/bars for instantaneous values (utilization, VRAM, power) using block characters with the same gradient.
- Dark theme by default, truecolor (24-bit) with a graceful fallback to 256 colors.
- Themes loadable from a simple file (TOML or btop-style `.theme`), with at least: `default`, `dracula`, `gruvbox`, `nord`.
- Mouse support (click a GPU panel to focus it, scroll the process list, click column headers to sort).
- Keyboard-driven, btop-like keys:
  - `q` / `Esc`: quit
  - `Tab` / `Shift+Tab` or `1-9`: focus GPU N
  - `a`: toggle "all GPUs" overview vs. single-GPU detail view
  - `s`: cycle process sort column, `r`: reverse sort
  - `+` / `-`: change refresh interval (100 ms to 5 s)
  - `t`: cycle theme
  - `f`: filter processes
  - `h` / `?`: help overlay
- Responsive layout: adapts to terminal resize, collapses gracefully on small terminals (minimum usable ~80x24).

## Data to display

Per GPU:

- Name, index, UUID (short), driver version, PCIe generation and width
- GPU utilization % (and memory-controller utilization %)
- VRAM used / total + percentage
- Temperature (core, plus memory/hotspot if available) and fan speed %
- Power draw / power limit (W) with a history graph
- Graphics, SM and memory clocks (current / max)
- PCIe TX/RX throughput
- Throttle reasons (power cap, thermal, etc.) shown as a small status indicator
- Encoder/decoder utilization if available

Per process (table, sortable):

- PID, user, command, GPU index, VRAM used, GPU SM % (if available), type (Graphics / Compute)

## Data sources

- **NVIDIA (primary):** NVML via `nvidia-ml-py` (`import pynvml`). Handle missing NVML gracefully (clear error message, exit code 1).
- Build a **backend abstraction** (`GPUBackend` base class with `get_devices()`, `sample(device)`, `get_processes(device)`) so AMD (`amdsmi` / sysfs hwmon) and Intel can be added later without touching the UI. Only implement NVIDIA now, but leave the AMD backend as a documented stub.
- Resolve PID -> username and command line through `psutil`.
- Sampling runs in a background thread/task, separate from rendering, so the UI never blocks on NVML calls.
- Keep a ring buffer of the last N samples per metric (N >= graph width) for the history graphs.
- Some metrics aren't supported on all cards or drivers (e.g. per-process utilization, power limit on laptops). Every NVML call must be wrapped so an `NVMLError_NotSupported` just hides that field instead of crashing.

## Tech stack

- **Python 3.11+**
- UI: **Textual** (or Rich `Live` + custom layout if Textual can't hit the btop look). Braille graph rendering should be a custom widget.
- Dependencies: `nvidia-ml-py`, `psutil`, `textual`. Nothing else unless justified.
- Packaging: `pyproject.toml`, installable with `pipx install .`, entry point `gputop`.

## Suggested project structure

```
gputop/
├── pyproject.toml
├── README.md
├── src/gputop/
│   ├── __main__.py          # CLI entry, arg parsing
│   ├── app.py               # Textual App, key bindings, layout switching
│   ├── config.py            # config file load/save (~/.config/gputop/config.toml)
│   ├── themes.py            # theme loading + built-in themes
│   ├── backends/
│   │   ├── base.py          # GPUBackend ABC, dataclasses (GPUInfo, GPUSample, GPUProcess)
│   │   ├── nvidia.py        # NVML implementation
│   │   └── amd.py           # stub, documented
│   ├── sampler.py           # background polling + ring buffers
│   └── widgets/
│       ├── braille_graph.py # btop-style braille history graph
│       ├── meter.py         # gradient bar meter
│       ├── gpu_panel.py     # one GPU's boxed panel
│       ├── process_table.py # sortable/filterable process list
│       └── help_overlay.py
└── tests/
    ├── test_braille_graph.py
    ├── test_sampler.py
    └── fake_backend.py      # fake multi-GPU backend for testing without hardware
```

## CLI

```
gputop [--interval MS] [--theme NAME] [--gpu INDEX[,INDEX...]] [--no-mouse] [--backend nvidia]
gputop --list      # print detected GPUs and exit
gputop --once      # print one snapshot as JSON and exit (useful for scripting)
```

## Layout sketch (all-GPUs view)

```
┌─ gputop ──────────────────────────────────────────── 500ms ─ 12:34:56 ┐
│ ┌─ GPU 0 · RTX 4070 ─────────────┐ ┌─ GPU 1 · RTX 3060 ────────────┐  │
│ │ util  ⣀⣄⣤⣦⣶⣷⣿⣿⣶⣤⣄  87%       │ │ util  ⣀⣀⣀⣄⣄⣀⣀⣀  12%          │  │
│ │ vram  ██████████░░  9.1/12 GB  │ │ vram  ██░░░░░░░░░░  1.2/12 GB │  │
│ │ power ⣄⣤⣦⣶⣶⣤⣄  178/200 W      │ │ power ⣀⣀⣀⣀⣀⣀  42/170 W       │  │
│ │ temp 71°C  fan 58%  2475 MHz   │ │ temp 38°C  fan 0%  210 MHz    │  │
│ └────────────────────────────────┘ └───────────────────────────────┘  │
│ ┌─ Processes ───────────────────────────────────────────────────────┐ │
│ │ PID    USER   COMMAND            GPU  VRAM     SM%   TYPE         │ │
│ │ 4211   nik    ollama             0    6.2 GB   81    Compute      │ │
│ │ 1337   nik    Xorg               0    0.4 GB   3     Graphics     │ │
│ └───────────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────┘
```

Single-GPU detail view (`a` to toggle): one GPU fills the screen with larger graphs for each metric (util, VRAM, power, temp, clocks, PCIe) and the process list underneath, filtered to that GPU.

## Quality requirements

- Idle CPU usage of the tool itself should stay low (target < 2% at 500 ms interval).
- No crash if a GPU disappears mid-run or a process exits between sampling and lookup.
- Works over SSH and inside tmux.
- Clean exit restores the terminal state.
- Unit tests for the braille graph renderer and the sampler's ring buffers, using the fake backend. The whole UI must be runnable with `--backend fake` so it can be developed on a machine without an NVIDIA GPU.

## Workflow for Claude Code

1. Plan the structure first, confirm it matches the tree above, then scaffold the package.
2. Implement the backend abstraction + fake backend + sampler, with tests.
3. Implement the braille graph and meter widgets, with tests.
4. Build the single-GPU panel, then multi-GPU layout, then the process table.
5. Add themes, key bindings, mouse, config file, help overlay.
6. Implement the real NVIDIA backend last and test it on real hardware.
7. Write the README with screenshots/asciicast instructions and an install section.

Ask me before adding any dependency beyond the three listed.
