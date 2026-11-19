"""AMD backend — documented stub, not implemented yet.

How to implement it
===================

Two data sources are viable; prefer ``amdsmi`` and fall back to sysfs.

1. **amdsmi** (``pip install amdsmi``, ships with ROCm >= 6)::

       import amdsmi
       amdsmi.amdsmi_init()
       handles = amdsmi.amdsmi_get_processor_handles()

   ===================  ===================================================
   GPUSample field      amdsmi call
   ===================  ===================================================
   util_gpu / util_mem  ``amdsmi_get_gpu_activity(h)`` -> gfx/umc_activity
   mem_used/mem_total   ``amdsmi_get_gpu_memory_usage/total(h, VRAM)``
   temp_core/hotspot    ``amdsmi_get_temp_metric(h, EDGE/HOTSPOT, CURRENT)``
   temp_mem             ``amdsmi_get_temp_metric(h, VRAM, CURRENT)``
   fan                  ``amdsmi_get_gpu_fan_speed(h, 0)`` / max * 100
   power_draw/limit     ``amdsmi_get_power_info(h)``
   clocks               ``amdsmi_get_clock_info(h, GFX / MEM)``
   pcie                 ``amdsmi_get_pcie_info(h)``
   processes            ``amdsmi_get_gpu_process_list(h)``
   ===================  ===================================================

2. **sysfs / hwmon** (no extra dependency; works with the in-kernel amdgpu
   driver)::

       /sys/class/drm/card*/device/gpu_busy_percent       -> util_gpu
       /sys/class/drm/card*/device/mem_busy_percent       -> util_mem
       /sys/class/drm/card*/device/mem_info_vram_{used,total}
       /sys/class/drm/card*/device/hwmon/hwmon*/temp{1,2,3}_input  (edge/junction/mem, m°C)
       /sys/class/drm/card*/device/hwmon/hwmon*/power1_average     (µW)
       /sys/class/drm/card*/device/hwmon/hwmon*/power1_cap         (µW)
       /sys/class/drm/card*/device/hwmon/hwmon*/pwm1 (/255)        -> fan
       /sys/class/drm/card*/device/pp_dpm_{sclk,mclk}  (line with '*' is current)
       /sys/class/drm/card*/device/current_link_{speed,width}

   Per-process VRAM can be read from ``/proc/<pid>/fdinfo/*`` entries with
   ``drm-driver: amdgpu`` (``drm-memory-vram``, ``drm-engine-gfx``).

Rules to follow (same as the NVIDIA backend):

* Return ``None`` for anything unsupported; never raise for a missing metric.
* Raise :class:`~gputop.backends.base.DeviceLostError` if the device vanishes.
* Raise :class:`~gputop.backends.base.BackendError` from :meth:`init` if the
  library/driver isn't available, with a message telling the user what to
  install.

Once implemented, nothing in the UI needs to change — register the class in
:func:`gputop.backends.create_backend` (already done below) and it works.
"""

from __future__ import annotations

from .base import BackendError, GPUBackend, GPUInfo, GPUProcess, GPUSample


class AMDBackend(GPUBackend):
    name = "amd"

    def init(self) -> None:
        raise BackendError(
            "The AMD backend is not implemented yet. "
            "See src/gputop/backends/amd.py for the implementation plan."
        )

    def get_devices(self) -> list[GPUInfo]:
        raise NotImplementedError

    def sample(self, device: GPUInfo) -> GPUSample:
        raise NotImplementedError

    def get_processes(self, device: GPUInfo) -> list[GPUProcess]:
        raise NotImplementedError
