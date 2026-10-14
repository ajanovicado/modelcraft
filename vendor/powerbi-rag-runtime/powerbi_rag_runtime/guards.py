"""Keep the Mac thresholds; fail closed when Windows telemetry is unavailable."""
import contextlib
import ctypes
from ctypes import wintypes
import json
import mmap
import os
from pathlib import Path
import shutil
import subprocess
import time

import psutil

GIB = 1024 ** 3


def resources(path):
    if os.name == 'nt':
        # psutil.sout is always zero on Windows. Read the cumulative native
        # raw counter, not a rate or a fabricated zero. This includes all page
        # output, a conservative guard relative to macOS swap-only output.
        from .windows_paging import pages_output
        pageouts = pages_output() * mmap.PAGESIZE
        class PowerStatus(ctypes.Structure):
            _fields_ = [('ac', wintypes.BYTE), ('flags', wintypes.BYTE),
                        ('percent', wintypes.BYTE), ('reserved', wintypes.BYTE),
                        ('life', wintypes.DWORD), ('full_life', wintypes.DWORD)]
        power = PowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(power)):
            raise OSError('Cannot read AC power status')
        ac = power.ac == 1  # unknown (255) fails closed, including no battery
    else:
        import re
        vm = subprocess.check_output(['/usr/bin/vm_stat'], text=True, timeout=5)
        pageouts = int(re.search(r'^Swapouts:\s*(\d+)\.', vm, re.M)[1]) * int(
            re.search(r'page size of (\d+) bytes', vm)[1])
        power = subprocess.check_output(['/usr/bin/pmset', '-g', 'batt'], text=True, timeout=5)
        ac = "Now drawing from 'AC Power'" in power
    return dict(available=psutil.virtual_memory().available,
                swap_used=psutil.swap_memory().used, native_swapouts=pageouts,
                ac=ac, disk_free=shutil.disk_usage(path).free)


def enforce(sample, baseline=None, *, startup=False):
    minimum = (8 if startup else 3) * GIB
    if not sample['ac'] or sample['available'] < minimum or sample['disk_free'] < minimum:
        raise RuntimeError('Resource guard (%s GiB RAM/disk, AC required): %s' %
                           (minimum // GIB, json.dumps(sample)))
    if baseline:
        for key in ('swap_used', 'native_swapouts'):
            if sample[key] < baseline[key] and key == 'native_swapouts':
                raise RuntimeError('Native paging counter reset; fail closed')
            if sample[key] - baseline[key] > 512 * 1024 ** 2:
                raise RuntimeError('New paging exceeded 512 MiB cap: ' + key)
    return sample


class Guard:
    def __init__(self, root):
        self.root = Path(root)
        self.started = time.monotonic()
        self.baseline = None

    def check(self, startup=False):
        if (self.root / 'STOP_REQUESTED').exists():
            raise RuntimeError('Stop requested')
        if time.monotonic() - self.started > 3600:
            raise RuntimeError('One-hour process cap reached')
        sample = enforce(resources(self.root), self.baseline, startup=startup)
        if self.baseline is None:
            self.baseline = sample
        return sample


@contextlib.contextmanager
def exclusive_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0, 2)
            if not stream.tell():
                stream.write(b'0'); stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield


@contextlib.contextmanager
def keep_awake():
    if os.name == 'nt':
        if not ctypes.windll.kernel32.SetThreadExecutionState(0x80000001):
            raise OSError('Failed to inhibit sleep')
        try:
            yield
        finally:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    else:
        process = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())])
        try:
            yield
        finally:
            process.terminate(); process.wait(timeout=5)
