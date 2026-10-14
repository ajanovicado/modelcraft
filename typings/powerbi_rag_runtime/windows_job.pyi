import ctypes
import subprocess
from typing import Protocol

class Basic(ctypes.Structure):
    flags: int

class Extended(ctypes.Structure):
    basic: Basic
    process_memory: int
    job_memory: int

class JobKernel(Protocol):
    def SetInformationJobObject(
        self, handle: int | None, info_class: int, info: object, size: int
    ) -> int: ...
    def CloseHandle(self, handle: int) -> int: ...

class MemoryJob:
    kernel: JobKernel
    handle: int | None
    def __init__(self, process: subprocess.Popen[bytes], limit: int) -> None: ...
    def close(self) -> None: ...
