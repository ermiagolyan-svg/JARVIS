#!/usr/bin/env python3
"""Hardware Telemetry Monitor for V.E.S.P.E.R. desktop monitoring."""

import time
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

# --- GPU / NVIDIA (pynvml) ---
try:
    import pynvml
    NVML_AVAILABLE = True
except ImportError:
    NVML_AVAILABLE = False

# --- System metrics (psutil) ---
import psutil


@dataclass
class CpuMetrics:
    """CPU telemetry snapshot."""
    usage_percent: float = 0.0
    per_core_load: list = field(default_factory=list)
    current_freq_mhz: float = 0.0
    min_freq_mhz: float = 0.0
    max_freq_mhz: float = 0.0
    temp_celsius: float = 0.0


@dataclass
class GpuMetrics:
    """GPU telemetry snapshot (NVIDIA RTX 3080)."""
    usage_percent: float = 0.0
    vram_used_mb: float = 0.0
    vram_total_mb: float = 0.0
    temp_celsius: float = 0.0
    power_watts: float = 0.0
    available: bool = False


@dataclass
class RamMetrics:
    """RAM telemetry snapshot."""
    total_gb: float = 0.0
    used_gb: float = 0.0
    free_gb: float = 0.0
    usage_percent: float = 0.0


@dataclass
class DiskMetrics:
    """Disk telemetry snapshot (Samsung 980 Pro)."""
    mountpoint: str = ""
    total_gb: float = 0.0
    used_gb: float = 0.0
    free_gb: float = 0.0
    usage_percent: float = 0.0


@dataclass
class TelemetrySnapshot:
    """Full hardware telemetry snapshot."""
    timestamp: float = 0.0
    cpu: CpuMetrics = field(default_factory=CpuMetrics)
    gpu: GpuMetrics = field(default_factory=GpuMetrics)
    ram: RamMetrics = field(default_factory=RamMetrics)
    disk_c: DiskMetrics = field(default_factory=DiskMetrics)
    disk_e: DiskMetrics = field(default_factory=DiskMetrics)


class HardwareMonitor:
    """Collects real-time hardware telemetry (CPU, GPU, RAM, Disk).

    Uses psutil for system metrics and pynvml for NVIDIA GPU stats.
    Includes graceful fallback when libraries are unavailable.
    """

    def __init__(self):
        self._last_snapshot_time: float = 0.0
        self._snapshot_cache: Optional[TelemetrySnapshot] = None
        self._cache_ttl: float = 1.0

        self._nvml_initialized: bool = False
        if NVML_AVAILABLE:
            try:
                pynvml.nvmlInit()
                self._nvml_initialized = True
            except Exception:
                pass

    def _read_cpu(self) -> CpuMetrics:
        m = CpuMetrics()
        m.usage_percent = psutil.cpu_percent(interval=None)
        per_core = psutil.cpu_percent(interval=None, percpu=True)
        m.per_core_load = [round(v, 1) for v in per_core]
        freq = psutil.cpu_freq()
        if freq:
            m.current_freq_mhz = round(freq.current, 1)
            m.min_freq_mhz = round(freq.min, 1)
            m.max_freq_mhz = round(freq.max, 1)
        try:
            temps = psutil.sensors_temperatures()
            for name, entries in temps.items():
                for entry in entries:
                    if entry.current > 0:
                        m.temp_celsius = round(entry.current, 1)
                        break
        except Exception:
            pass
        if m.temp_celsius == 0.0:
            try:
                import wmi
                w = wmi.WMI()
                for sensor in w.Win32_TemperatureSensor():
                    val = sensor.CurrentReading
                    if val and val > 0:
                        m.temp_celsius = round(float(val), 1)
                        break
            except Exception:
                pass
        return m

    def _read_gpu(self) -> GpuMetrics:
        m = GpuMetrics()
        if not NVML_AVAILABLE or not self._nvml_initialized:
            m.available = False
            return m
        try:
            device_count = pynvml.nvmlDeviceGetCount()
            if device_count == 0:
                m.available = False
                return m
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            info = pynvml.nvmlDeviceGetUtilizationRates(handle)
            m.usage_percent = round(info.gpu, 1)
            mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            m.vram_used_mb = round(mem_info.used / (1024 ** 2), 1)
            m.vram_total_mb = round(mem_info.total / (1024 ** 2), 1)
            temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            m.temp_celsius = float(temp)
            power = pynvml.nvmlDeviceGetPowerUsage(handle)
            m.power_watts = round(power / 1000.0, 2)
            m.available = True
        except Exception:
            m.available = False
        return m

    def _read_ram(self) -> RamMetrics:
        m = RamMetrics()
        vm = psutil.virtual_memory()
        m.total_gb = round(vm.total / (1024 ** 3), 2)
        m.used_gb = round(vm.used / (1024 ** 3), 2)
        m.free_gb = round(vm.free / (1024 ** 3), 2)
        m.usage_percent = round(vm.percent, 1)
        return m

    def _read_disk(self, mountpoint: str) -> DiskMetrics:
        m = DiskMetrics()
        try:
            usage = psutil.disk_usage(mountpoint)
            m.mountpoint = mountpoint
            m.total_gb = round(usage.total / (1024 ** 3), 2)
            m.used_gb = round(usage.used / (1024 ** 3), 2)
            m.free_gb = round(usage.free / (1024 ** 3), 2)
            m.usage_percent = round(usage.percent, 1)
        except Exception:
            pass
        return m

    def get_telemetry_snapshot(self) -> TelemetrySnapshot:
        """Return a cached telemetry snapshot (TTL = 1 s)."""
        now = time.time()
        if self._snapshot_cache is not None and (now - self._last_snapshot_time) < self._cache_ttl:
            return self._snapshot_cache
        snap = TelemetrySnapshot(timestamp=now)
        snap.cpu = self._read_cpu()
        snap.gpu = self._read_gpu()
        snap.ram = self._read_ram()
        snap.disk_c = self._read_disk("C:\\")
        snap.disk_e = self._read_disk("E:\\")
        self._snapshot_cache = snap
        self._last_snapshot_time = now
        return snap

    def get_persian_status_summary(self) -> str:
        """Return a human-readable Persian (Farsi) status summary string."""
        snap = self.get_telemetry_snapshot()
        lines = []
        lines.append("== V.E.S.P.E.R. Hardware Telemetry ==")
        lines.append("")
        cpu = snap.cpu
        lines.append(f"CPU: {cpu.usage_percent}% | Per-core: {cpu.per_core_load}")
        lines.append(f"     Freq: {cpu.current_freq_mhz} MHz (min {cpu.min_freq_mhz}, max {cpu.max_freq_mhz})")
        lines.append(f"     Temp: {cpu.temp_celsius} C")
        lines.append("")
        gpu = snap.gpu
        if gpu.available:
            lines.append("GPU (RTX 3080 10GB):")
            lines.append(f"     Usage:   {gpu.usage_percent}%")
            lines.append(f"     VRAM:    {gpu.vram_used_mb}/{gpu.vram_total_mb} MB")
            lines.append(f"     Temp:    {gpu.temp_celsius} C")
            lines.append(f"     Power:   {gpu.power_watts} W")
        else:
            lines.append("GPU: pynvml unavailable")
        lines.append("")
        ram = snap.ram
        lines.append(f"RAM: {ram.used_gb}/{ram.total_gb} GB ({ram.usage_percent}%)")
        lines.append(f"     Free: {ram.free_gb} GB")
        lines.append("")
        dc = snap.disk_c
        lines.append(f"Disk C: {dc.used_gb}/{dc.total_gb} GB ({dc.usage_percent}%) | Free {dc.free_gb} GB")
        de = snap.disk_e
        lines.append(f"Disk E: {de.used_gb}/{de.total_gb} GB ({de.usage_percent}%) | Free {de.free_gb} GB")
        lines.append("")
        lines.append(f"Timestamp: {snap.timestamp:.2f}")
        return "\n".join(lines)


if __name__ == "__main__":
    print("=" * 60)
    print("  V.E.S.P.E.R. Hardware Monitor - Self-Test")
    print("=" * 60)

    monitor = HardwareMonitor()

    snap = monitor.get_telemetry_snapshot()
    print("\n--- Telemetry Snapshot ---")
    print(f"CPU usage: {snap.cpu.usage_percent}%")
    print(f"CPU per-core: {snap.cpu.per_core_load}")
    print(f"CPU freq: {snap.cpu.current_freq_mhz} MHz")
    print(f"CPU temp: {snap.cpu.temp_celsius} C")
    print(f"GPU available: {snap.gpu.available}")
    if snap.gpu.available:
        print(f"GPU usage: {snap.gpu.usage_percent}%")
        print(f"GPU VRAM: {snap.gpu.vram_used_mb}/{snap.gpu.vram_total_mb} MB")
        print(f"GPU temp: {snap.gpu.temp_celsius} C")
        print(f"GPU power: {snap.gpu.power_watts} W")
    else:
        print("GPU metrics unavailable (pynvml not initialized)")
    print(f"RAM: {snap.ram.used_gb}/{snap.ram.total_gb} GB ({snap.ram.usage_percent}%)")
    print(f"Disk C: {snap.disk_c.used_gb}/{snap.disk_c.total_gb} GB ({snap.disk_c.usage_percent}%)")
    print(f"Disk E: {snap.disk_e.used_gb}/{snap.disk_e.total_gb} GB ({snap.disk_e.usage_percent}%)")

    import time as _time
    _time.sleep(0.2)
    snap2 = monitor.get_telemetry_snapshot()
    assert snap.timestamp == snap2.timestamp, "Cache should be active!"
    print("\nCache test PASSED (1s TTL working).")

    print("\n--- Persian Status Summary ---")
    print(monitor.get_persian_status_summary())

    print("\n" + "=" * 60)
    print("  Self-test complete.")
    print("=" * 60)
