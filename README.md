# gputop

A **btop-style multi-GPU terminal monitor** for Linux. Think "btop, but the
whole screen is GPU stats": rounded boxes, braille history graphs with
green → yellow → red gradients, gradient meters, a sortable process list, mouse
support and themes. It handles any number of GPUs at once.

```
╭─ gputop 2 GPUs ─────────────────────────── nvidia 555.42 ─ 500ms ─ 12:34:56 ─╮
│╭─ ¹GPU 0 · RTX 4090 ────────────────╮ ╭─ ²GPU 1 · RTX 3060 ─────────────────╮│
││ util  ⣶⣶⡆   ⣶⣶⣶⣶⣶⣶⣶⣶⣶⣶         89% │ │ util     ⢰⣴⣦⣤⣤⣀⣀⣤                4% ││
││       ⣿⣿⣧⣄⣤⣤⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿             │ │       ⣀⣀⣀⣸⣿⣿⣿⣿⣿⣿⣿⣀⣀⣄⣠⣄⣀             ││
││ vram  █████▌░░░░░░░░░░ 8.3/24.0 GB │ │ vram  █▋░░░░░░░░░░░░░░░ 1.2/12.0 GB ││
││ power ⣶⣶⡆   ⣶⣶⣾⣶⣶⣶⣶⣶⣾⣶    395/450W │ │ power    ⢰⣶⣶⣦⣶⣤⣤⣤           18/170W ││
││ temp 74°C  mem 82°C  fan 54%       │ │ temp 31°C  mem 39°C  fan 0%  315MHz ││
││ pcie  ↓2.5 GB/s  ↑1.6 GB/s         │ │ pcie  ↓125 MB/s  ↑80.6 MB/s         ││
│╰──────────────────────────── ⚠ PWR ─╯ ╰────────────────────────────── idle ─╯│
│╭─ processes ────────────────────────────────────────────────────────────────╮│
││ PID     USER      COMMAND                       GPU    VRAM▼  SM% TYPE     ││
││ 4211    nik       ollama serve                    0     6.2G   81 Compute  ││
││ 1337    nik       /usr/lib/Xorg :0                0   412.0M    3 Graphics ││
│╰────────────────────────────────────────────────────────────────── 2 procs ─╯│
╰────────────────────────── h help  a view  s sort  f filter  t theme  q quit ─╯
```

## Features

- **All-GPUs overview**: one boxed panel per GPU, laid out in a grid that adapts
  to the terminal size. Panels grow taller graphs on big terminals and collapse
  to one or two lines on small ones (usable from 80x24; works down to ~40x10).
- **Single-GPU detail view** (`a`): six large graphs (utilisation, VRAM, power,
  temperature, clocks, PCIe RX/TX mirrored) plus the process list filtered to
  that GPU.
- **Per GPU**: name, index, short UUID, driver, PCIe generation/width (current and
  max), GPU and memory-controller utilisation, VRAM, core/memory temperature, fan,
  power draw/limit, graphics/SM/memory clocks, PCIe throughput, throttle reasons
  (power cap, thermal, …) and encoder/decoder utilisation.
- **Processes**: PID, user, command, GPU, VRAM, SM %, type (Compute / Graphics /
  C+G). Sort by any column, reverse, filter by text, scroll.
- Metrics a card or driver doesn't support (per-process utilisation, power limit
  on laptops, fan on passively cooled cards…) are simply hidden.
- Truecolor with automatic 256-colour fallback; works over SSH and in tmux.
- Themes: `default`, `dracula`, `gruvbox`, `nord`, plus your own (btop `.theme`
  files work as-is).

## Install

Requires Python 3.11+ and Linux. For real data you need the proprietary NVIDIA
driver (which provides `libnvidia-ml.so`).

```sh
pipx install .            # from a checkout of this repository
gputop
```

Or in a virtualenv: `pip install .`. Dependencies: `nvidia-ml-py`, `psutil`,
`textual`.

No NVIDIA GPU? Try it with synthetic data:

```sh
gputop --backend fake --fake-gpus 4
```

## Usage

```
gputop [--interval MS] [--theme NAME] [--gpu INDEX[,INDEX...]] [--no-mouse]
       [--backend {nvidia,fake,amd}] [--color {auto,truecolor,256,16}]
gputop --list      # print detected GPUs and exit
gputop --once      # print one snapshot as JSON and exit (for scripting)
```

`--once` output is a JSON object with `timestamp`, `backend` and a `gpus` list;
each entry has `info` (static facts), `sample` (all metrics, `null` when
unsupported; bytes, watts, MHz, °C, %) and `processes`.

```sh
gputop --once | jq '.gpus[] | {name: .info.name, util: .sample.util_gpu}'
```

Exit codes: `0` ok, `1` no usable backend/GPU (e.g. NVML missing), `2` bad
arguments.

### Keys

| Key | Action |
| --- | --- |
| `q` / `Esc` | quit |
| `Tab` / `Shift+Tab` | focus next / previous GPU |
| `1`–`9` | focus GPU panel N (the superscript number in each panel title) |
| `a` / `Enter` | toggle all-GPUs overview ↔ single-GPU detail view |
| `↑` `↓` `PgUp` `PgDn` `Home` `End` | select process |
| `s` / `r` | cycle sort column / reverse sort |
| `f` (or `/`) | filter processes by command, user or PID (`Enter` keeps, `Esc` clears) |
| `+` / `-` | refresh faster / slower (100 ms – 5 s) |
| `t` | cycle theme |
| `h` / `?` | help overlay |

**Mouse**: click a GPU panel to focus it, double-click to open its detail view,
click a column header to sort (click again to reverse), scroll the wheel over
the process list. Disable with `--no-mouse`.

## Configuration

`~/.config/gputop/config.toml` (respects `$XDG_CONFIG_HOME`):

```toml
interval_ms = 500
theme = "default"
mouse = true
backend = "nvidia"
sort_key = "vram"      # pid, user, command, gpu, vram, sm, type
sort_reverse = false
view = "all"           # or "detail"
color = "auto"         # auto, truecolor, 256, 16
```

Settings you change *inside the app* (theme, interval, sort, view) are saved on
exit. Command-line flags only apply to that run.

### Themes

Drop files into `~/.config/gputop/themes/`. The file name is the theme name.
btop theme files work unchanged:

```
# ~/.config/gputop/themes/mocha.theme
theme[main_bg]="#1e1e2e"
theme[main_fg]="#cdd6f4"
theme[hi_fg]="#f38ba8"
theme[cpu_box]="#89b4fa"
theme[cpu_start]="#a6e3a1"
theme[cpu_mid]="#f9e2af"
theme[cpu_end]="#f38ba8"
```

or TOML (`mocha.toml`) with the same keys under `[colors]`. Colours can be
`#rrggbb`, `#gg` (grey) or `"r g b"`. Missing keys fall back to `default`.
`--theme` also accepts a path to a theme file.

| Gradient | Keys | Used for |
| --- | --- | --- |
| util | `cpu_start/mid/end` | utilisation, clocks |
| vram / power | `vram_*` / `power_*` (fall back to `cpu_*`) | VRAM and power bars/graphs |
| temp | `temp_*` | temperatures, fan |
| rx / tx | `download_*` / `upload_*` | PCIe throughput |
| proc | `process_*` | process SM % |

Box colours: `cpu_box` (GPU panels), `proc_box` (process list), `hi_fg`
(focused panel, highlights), `graph_text` (outer frame, secondary text).

## Screenshots / recordings

Use any terminal screenshot tool, or record an asciicast:

```sh
pip install asciinema            # or your distro package
asciinema rec -c "gputop --backend fake --fake-gpus 3" gputop.cast
asciinema play gputop.cast       # or upload: asciinema upload gputop.cast
# convert to GIF with agg: https://github.com/asciinema/agg
agg gputop.cast gputop.gif
```

The `fake` backend makes recordings reproducible and needs no GPU.

## Architecture

```
src/gputop/
├── __main__.py          CLI, --list / --once, starts the TUI
├── app.py               Textual app: layout, key bindings, view switching
├── config.py            config file load/save
├── themes.py            built-in themes, btop/TOML theme loading, gradients
├── fmt.py               unit formatting
├── procinfo.py          PID -> user/command via psutil (cached)
├── sampler.py           background polling thread + ring buffers
├── backends/
│   ├── base.py          GPUBackend ABC + GPUInfo / GPUSample / GPUProcess
│   ├── nvidia.py        NVML implementation
│   ├── fake.py          synthetic multi-GPU backend (--backend fake)
│   └── amd.py           documented stub with an implementation plan
└── widgets/
    ├── braille_graph.py braille history graph renderer + widget
    ├── meter.py         gradient bar meter
    ├── gpu_panel.py     GPU panel, detail view, metric boxes
    ├── process_table.py sortable/filterable process list
    ├── help_overlay.py  help screen
    └── common.py        Line-API base widget and small helpers
```

- The **sampler thread** polls the backend at the refresh interval and posts a
  message to the UI, so slow vendor calls never block rendering (NVML's PCIe
  throughput query alone blocks ~20 ms per call).
- Each metric keeps a **ring buffer** sized to at least twice the terminal width
  (braille packs two samples per cell).
- Every NVML call is wrapped: "not supported" / "no permission" just hide a
  field; "GPU is lost" marks that GPU as lost (its panel says so) while the
  others keep updating. A process that exits between NVML reporting it and the
  psutil lookup is shown as `?` / `[pid]` rather than crashing.
- **Adding a vendor** means writing one `GPUBackend` subclass; nothing in the UI
  changes. See `backends/amd.py` for the AMD plan (amdsmi or sysfs/hwmon).

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
pytest                                   # unit + headless UI tests, no GPU needed
gputop --backend fake --fake-gpus 3      # run the UI on synthetic data
```

The test suite covers the braille renderer, meters, ring buffers and the
sampler (including GPU loss and vanished processes), themes, config, the CLI,
and the UI itself driven headless through Textual's pilot (keys, mouse, resize,
filter, detail view, GPU loss mid-run).

### Performance

gputop renders through Textual's Line API and only repaints what changed. On an
Intel i7-4870HQ (2014) at a 500 ms interval with the fake backend, the gputop
process measured:

| Terminal | GPUs | CPU |
| --- | --- | --- |
| 80x24 | 2 | ~2–2.5 % |
| 140x40 | 3 | ~2.9 % |
| 200x50 | 4 | ~3.5 % |

Cost scales with the number of cells redrawn per tick. At 1 s it's about half
that, and at 5 s it's ~0.6 %. 256-colour mode costs slightly more because
colours get converted on the fly. On newer CPUs expect lower numbers. Use `-` to
slow the refresh on very large terminals.

## License

MIT
