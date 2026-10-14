"""Read Windows' cumulative page-output count directly with PDH (no shell)."""
import atexit
import ctypes
from ctypes import wintypes
import threading


class RawCounter(ctypes.Structure):
    _fields_ = [('status', wintypes.DWORD), ('timestamp', wintypes.FILETIME),
                ('first', ctypes.c_longlong), ('second', ctypes.c_longlong),
                ('multiple', wintypes.DWORD)]


class PageCounter:
    def __init__(self):
        self.dll = ctypes.WinDLL('pdh')
        handle = wintypes.HANDLE
        self.dll.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(handle)]
        self.dll.PdhAddEnglishCounterW.argtypes = [handle, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(handle)]
        self.dll.PdhCollectQueryData.argtypes = [handle]
        self.dll.PdhGetRawCounterValue.argtypes = [handle, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(RawCounter)]
        self.dll.PdhCloseQuery.argtypes = [handle]
        self.query = handle()
        self.counter = handle()
        self.lock = threading.Lock()
        self._ok(self.dll.PdhOpenQueryW(None, 0, ctypes.byref(self.query)))
        try:
            self._ok(self.dll.PdhAddEnglishCounterW(self.query,
                r'\Memory\Pages Output/sec', 0, ctypes.byref(self.counter)))
        except BaseException:
            self.dll.PdhCloseQuery(self.query)
            raise
        atexit.register(self.close)

    @staticmethod
    def _ok(status):
        if status != 0:
            raise OSError('Native paging telemetry failed: PDH status 0x%08x' % (status & 0xffffffff))

    def read(self):
        with self.lock:
            self._ok(self.dll.PdhCollectQueryData(self.query))
            result = RawCounter()
            kind = wintypes.DWORD()
            self._ok(self.dll.PdhGetRawCounterValue(self.counter, ctypes.byref(kind), ctypes.byref(result)))
            if result.status not in (0, 1) or result.first < 0:
                raise OSError('Native paging counter is unavailable')
            return result.first

    def close(self):
        if self.query:
            self.dll.PdhCloseQuery(self.query)
            self.query = None


_counter = None
_init_lock = threading.Lock()


def pages_output():
    global _counter
    with _init_lock:
        if _counter is None:
            _counter = PageCounter()
    return _counter.read()
