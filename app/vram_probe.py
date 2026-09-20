"""Best-effort check for whether a contiguous VRAM block is actually
allocatable before handing a job to the Starlight/neuroserver generative
engine -- confirmed live on this machine that `nvidia-smi`'s and the CUDA
driver API's own `cuMemGetInfo`'s "free" numbers can both be wildly optimistic
under Windows/WDDM (cuMemGetInfo reported 22.79 GiB free while nvidia-smi
showed 6.74 GiB free with a real job running), so neither is trustworthy on
its own. Actually attempting the allocation is: a real `cuMemAlloc` request
correctly fails with CUDA_ERROR_OUT_OF_MEMORY when the space genuinely isn't
available, confirmed by testing both a fitting and an oversized request
against a live GPU.

Uses the CUDA driver API directly via ctypes against `nvcuda.dll` (ships with
every NVIDIA driver) rather than torch/pynvml -- neither is a dependency of
this project, and neuroserver's own bundled torch install isn't invokable
standalone (its venv shim has no pyvenv.cfg, confirmed by testing)."""
from __future__ import annotations

import ctypes
import logging

logger = logging.getLogger(__name__)

_CUDA_SUCCESS = 0


def can_allocate(gb: float) -> bool:
    """True if a contiguous block of `gb` GiB can actually be allocated (and
    is immediately freed again) on the default GPU right now. Any failure in
    the probe machinery itself (missing DLL, driver call errors, no GPU) logs
    a warning and returns True -- an infra hiccup in the checker must never
    be the reason a real, otherwise-fine job gets parked as needs_restart."""
    try:
        return _try_allocate(gb)
    except Exception as e:  # noqa: BLE001 -- fail open, see docstring
        logger.warning("vram_probe.can_allocate(%s) failed internally, assuming OK: %s", gb, e)
        return True


def _try_allocate(gb: float) -> bool:
    cuda = ctypes.WinDLL("nvcuda.dll")
    cuda.cuMemAlloc_v2.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t]

    def check(rc: int, what: str) -> None:
        if rc != _CUDA_SUCCESS:
            raise RuntimeError(f"{what} failed: rc={rc}")

    check(cuda.cuInit(0), "cuInit")
    dev = ctypes.c_int()
    check(cuda.cuDeviceGet(ctypes.byref(dev), 0), "cuDeviceGet")
    ctx = ctypes.c_void_p()
    check(cuda.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev), "cuCtxCreate_v2")
    try:
        size = int(gb * 1024 ** 3)
        ptr = ctypes.c_void_p()
        rc = cuda.cuMemAlloc_v2(ctypes.byref(ptr), size)
        if rc != _CUDA_SUCCESS:
            return False
        cuda.cuMemFree_v2(ptr)
        return True
    finally:
        cuda.cuCtxDestroy_v2(ctx)
