"""Windows job objects for browser turns: a harness and every process it starts live in one
job, so the tree can be terminated as a unit and dies with the job handle (and therefore with
the server) however the parent exits. The harness is created suspended, adopted, then resumed,
so nothing can be spawned outside the job. Windows only."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

CREATE_SUSPENDED = 0x00000004          # process creation flag; subprocess does not export it
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9
THREAD_SUSPEND_RESUME = 0x0002
TH32CS_SNAPTHREAD = 0x0004


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                     "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD)]


_k32.CreateJobObjectW.restype = wintypes.HANDLE
_k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
_k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
_k32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
_k32.OpenThread.restype = wintypes.HANDLE
_k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.ResumeThread.argtypes = [wintypes.HANDLE]
_k32.ResumeThread.restype = wintypes.DWORD


def adopt(proc):
    """Put a (suspended) Popen process into a new kill-on-close job. Returns the job handle,
    or raises OSError when the process could not be assigned (the caller then resumes it and
    falls back to taskkill)."""
    job = _k32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not _k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(limits), ctypes.sizeof(limits)) \
            or not _k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle))):
        err = ctypes.get_last_error()
        _k32.CloseHandle(job)
        raise ctypes.WinError(err)
    return job


def resume(proc):
    """Resume every thread of a process started with CREATE_SUSPENDED (a new process has one)."""
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    resumed = 0
    try:
        entry = _ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _k32.Thread32First(snap, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == proc.pid:
                handle = _k32.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if handle:
                    if _k32.ResumeThread(handle) != 0xFFFFFFFF:
                        resumed += 1
                    _k32.CloseHandle(handle)
            ok = _k32.Thread32Next(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    if not resumed:       # a harness left suspended would sit until the timeout: refuse instead
        raise OSError(f"no thread of process {proc.pid} could be resumed")


def terminate(job):
    _k32.TerminateJobObject(job, 1)


def close(job):
    _k32.CloseHandle(job)
