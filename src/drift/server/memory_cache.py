"""
A pytorch memory cache that can be allocated by ConnectionHandler (on cpu) and used over multiple calls to Runtime.

For now, the only purpose of this code is to ensure that allocated memory will be deleted properly.

"""
import asyncio
import contextlib
import ctypes
import math
import multiprocessing as mp
import os
import threading
import time
from typing import AsyncContextManager, Dict, List, Optional, Sequence

import async_timeout
import torch
from hivemind.utils import TensorDescriptor, enter_asynchronously, get_logger

from drift.data_structures import Handle
from drift.utils.asyncio import shield_and_wait
from drift.utils.misc import get_size_in_bytes

logger = get_logger(__name__)


# A wire shape must not expand into arbitrarily many host-side page-table rows.
# This matches the largest default task-token limit, independently of GPU pages.
MAX_PAGED_BATCH_SIZE = 8192


def validate_paged_batch_size(batch_size: int, *, max_rows: int = MAX_PAGED_BATCH_SIZE) -> None:
    limit = min(MAX_PAGED_BATCH_SIZE, max_rows)
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= limit:
        raise ValueError("Paged cache batch size exceeds the supported row budget")


class MemoryCache:
    """A shared cache for storing tensors that persist across calls. Main use case: storing past attention KVs"""

    def __init__(
        self,
        max_size_bytes: Optional[int],
        max_alloc_timeout: Optional[float] = None,
        *,
        paged: bool = False,
        page_size: int = 16,
    ):
        self.max_size_bytes = max_size_bytes if max_size_bytes is not None else (2**64 - 1)
        self.max_alloc_timeout = max_alloc_timeout
        self._lock_metadata = mp.Lock()
        self._current_size = mp.Value(ctypes.c_int64, 0, lock=False)
        self._enqueued_size = mp.Value(ctypes.c_int64, 0, lock=True)
        self._handle_counter = mp.Value(ctypes.c_int64, 0, lock=False)
        self._allocated_tensors: Dict[Handle, torch.Tensor] = {}
        self.runtime_pid = os.getpid()
        self.runtime_thread_id: Optional[int] = None

        self._pipe_recv, self._pipe_send = mp.Pipe(duplex=False)  # any ConnectionHandler -> runtime
        self._lock_acquire_memory = mp.Lock()
        self._memory_freed_event = mp.Event()

        # Paged mode: sessions draw fixed-size pages lazily from a shared pool instead of reserving
        # a contiguous max_length cache up front (see PagedKVPool). The pool itself is created on the
        # runtime side; handlers only do page-budget admission via the shared counters below.
        self.paged = paged
        self.page_size = page_size
        self._paged_pool: "Optional[PagedKVPool]" = None  # runtime-side only
        self._paged_pool_config: Optional[dict] = None
        self._num_pages = mp.Value(ctypes.c_int64, 0, lock=False)
        self._paged_used_pages = mp.Value(ctypes.c_int64, 0, lock=True)

    @property
    def current_size_bytes(self) -> int:
        return self._current_size.value

    @current_size_bytes.setter
    def current_size_bytes(self, value: int):
        self._current_size.value = value

    @property
    def enqueued_size_bytes(self) -> int:
        return self._enqueued_size.value

    @enqueued_size_bytes.setter
    def enqueued_size_bytes(self, value: int):
        self._enqueued_size.value = value

    @property
    def bytes_left(self) -> int:
        return self.max_size_bytes - self.current_size_bytes

    @property
    def handle_counter(self) -> int:
        return self._handle_counter.value

    @handle_counter.setter
    def handle_counter(self, value: int):
        self._handle_counter.value = value

    @contextlib.asynccontextmanager
    async def allocate_cache(
        self, *descriptors: TensorDescriptor, timeout: float
    ) -> AsyncContextManager[Sequence[Handle]]:
        """
        Create a handle that is associated with buffers on unique device. If cache full, raises AllocationFailed.

        :param descriptors: one or more tensors tensor of this size, dtype, etc
        :param timeout: optional maximum time to wait for cache allocation; None (default) means no time limit

        :note: if descriptors reside on different devices, it is expected that they are approximately balanced across devices;
          if not, it will count maximum tensor allocation across devices for the purposes of size limit

        :note: This function should be called by connection handlers, it can be called concurrently from multiple processes.
        Furthermore, it can be called concurrently with at most one use_cache call in runtime.
        """
        assert not self._is_runtime_context(), "must be called by a ConnectionHandler, not runtime"
        assert all(descr.device is not None for descr in descriptors), "please specify allocated devices"
        if self.max_alloc_timeout is not None:
            timeout = min(timeout, self.max_alloc_timeout)
        max_alloc_size = self.get_allocation_size(*descriptors)

        gib = 1024**3
        cur_size, max_size = self.current_size_bytes, self.max_size_bytes
        friendly_max_size = f"{max_size / gib:.2f}" if max_size != 2**64 - 1 else "inf"
        logger.info(
            f"rpc_inference.wait_for_alloc(size={max_alloc_size / gib:.2f} GiB), "
            f"already used {cur_size / gib:.2f}/{friendly_max_size} GiB ({cur_size / max_size * 100:.1f}%)"
        )

        alloc_task = asyncio.create_task(self._schedule_alloc(max_alloc_size, *descriptors, timeout=timeout))
        try:
            handles = await shield_and_wait(alloc_task)
            logger.info(f"rpc_inference.alloc_done(size={max_alloc_size / gib:.2f} GiB)")
            yield handles
        finally:
            self._free(max_alloc_size, alloc_task)

    @staticmethod
    def get_allocation_size(*descriptors: TensorDescriptor) -> int:
        """Return the memory size (bytes) to be allocated on a device. If there are many devices, return maximum"""
        alloc_size_by_device = {}
        for descr in descriptors:
            tensor_size = descr.numel() * get_size_in_bytes(descr.dtype)
            alloc_size_by_device[descr.device] = alloc_size_by_device.get(descr.device, 0) + tensor_size
        return max(alloc_size_by_device.values())

    async def _schedule_alloc(
        self, alloc_size: int, *descriptors: TensorDescriptor, timeout: Optional[float]
    ) -> Sequence[Handle]:
        """
        This method should be called inside asyncio.shield() because:
            - hivemind.utils.enter_asynchronously() does not always release the lock on cancellation
        """
        try:
            async with self._wait_for_free_memory(alloc_size, timeout):
                with self._lock_metadata:
                    handles = tuple(int(self.handle_counter) + i for i in range(len(descriptors)))
                    self.current_size_bytes += alloc_size
                    self.handle_counter += len(handles)  # note: this will eventually overflow and it is okay
                    self._pipe_send.send((handles, descriptors))
                    return handles
        except TimeoutError:
            raise AllocationFailed(f"Could not allocate {alloc_size} (timeout={timeout})")

    @contextlib.asynccontextmanager
    async def _wait_for_free_memory(self, alloc_size: int, timeout: Optional[float]):
        start_time = time.perf_counter()
        loop = asyncio.get_event_loop()

        with self._enqueued_size.get_lock():
            self._enqueued_size.value += alloc_size
        allocated = False
        try:
            context_manager = async_timeout.timeout(timeout) if timeout != 0 else contextlib.AsyncExitStack()
            # contextlib.AsyncExitStack() is used as a null context here
            async with context_manager:
                if timeout == 0 and self.current_size_bytes + self.enqueued_size_bytes > self.max_size_bytes:
                    raise AllocationFailed(f"Could not allocate {alloc_size} bytes immediately: out of memory")
                async with enter_asynchronously(self._lock_acquire_memory):
                    if self.current_size_bytes + alloc_size > self.max_size_bytes:
                        if timeout == 0:
                            raise AllocationFailed(f"Could not allocate {alloc_size} bytes immediately: out of memory")
                        elapsed_time = time.perf_counter() - start_time
                        remaining_timeout = max(0.0, timeout - elapsed_time) if timeout is not None else None
                        await loop.run_in_executor(None, self._wait_until_available, alloc_size, remaining_timeout)

                allocated = True
                with self._enqueued_size.get_lock():
                    self._enqueued_size.value -= alloc_size
                yield
        except asyncio.TimeoutError:
            raise AllocationFailed(f"Could not allocate {alloc_size} within {timeout} seconds")
        finally:
            if not allocated:
                with self._enqueued_size.get_lock():
                    self._enqueued_size.value -= alloc_size

    def _free(self, alloc_size: int, alloc_task: asyncio.Task):
        if alloc_task.exception() is not None:
            return
        handles = alloc_task.result()

        with self._lock_metadata:
            self._pipe_send.send((handles, None))  # signal runtime to free these handles
            self.current_size_bytes -= alloc_size
        self._memory_freed_event.set()

    def _wait_until_available(self, allocated_size: int, timeout: Optional[float] = None):
        # note: this function should only be called inside _lock_acquire_memory!
        if allocated_size > self.max_size_bytes:
            raise AllocationFailed(
                f"Could not allocate {allocated_size} bytes, max cache size = {self.max_size_bytes} bytes"
            )
        timeout = timeout if timeout != float("inf") else None
        deadline = None if timeout is None else time.perf_counter() + timeout
        while self.current_size_bytes + allocated_size > self.max_size_bytes:
            remaining_time = None if timeout is None else deadline - time.perf_counter()
            if not self._memory_freed_event.wait(remaining_time):
                raise AllocationFailed(
                    f"Server's attention cache is full, failed to allocate {allocated_size} bytes in {timeout} seconds"
                )
            self._memory_freed_event.clear()

    @contextlib.contextmanager
    def use_cache(self, *handles: Handle) -> Sequence[torch.Tensor]:
        """
        Return one or more tensors previously allocated with allocate_cache,

        :note: This method is called by ModuleBackend in runtime: a single process with NO process parallelism.
        However, runtime may call use_cache concurrently with one or more connection handlers calling allocate_cache
        """
        assert os.getpid() == self.runtime_pid
        if self.runtime_thread_id is None:
            self.runtime_thread_id = threading.get_ident()
        assert self._is_runtime_context()
        # note: this specific function is not concurrent, so you can safely allocate/offload/defragment data here

        # read creation/deletion requests from connection handlers
        while self._pipe_recv.poll():
            recv_handles, recv_data = self._pipe_recv.recv()
            if recv_data is not None:  # create new tensors
                assert len(recv_handles) == len(recv_data)
                for handle, descr in zip(recv_handles, recv_data):
                    self._allocated_tensors[handle] = descr.make_zeros()
                    assert handle in self._allocated_tensors, f"Sanity check failed: no such handle ({handle})"
            else:  # delete tensors by handle
                for handle in recv_handles:
                    if handle not in self._allocated_tensors:
                        logger.warning(
                            f"Sanity check failed: asked to delete handle {handle}, but there is no such handle"
                        )
                    self._allocated_tensors.pop(handle, None)
        yield tuple(self._allocated_tensors[handle] for handle in handles)

    def mark_runtime_thread(self) -> None:
        """Claim the calling thread as the runtime thread. Called once at runtime startup so that
        the runtime is identifiable before its first ``use_cache`` -- important on Windows thread-mode,
        where connection handlers share the runtime's pid and can only be told apart by thread id."""
        assert os.getpid() == self.runtime_pid
        self.runtime_thread_id = threading.get_ident()

    def _is_runtime_context(self) -> bool:
        if os.getpid() != self.runtime_pid:
            return False  # a different process => a connection handler (POSIX fork)
        # Same pid: on POSIX this is the runtime; on Windows thread-mode it may be the runtime or a
        # handler thread. Once the runtime has claimed its thread we can tell them apart; before that
        # (e.g. no runtime started, as in unit tests) a same-pid caller is treated as the runtime.
        if self.runtime_thread_id is None:
            return True
        return threading.get_ident() == self.runtime_thread_id

    # ---- Paged mode (opt-in; see PagedKVPool) --------------------------------------------------

    def configure_paged_pool(
        self,
        *,
        num_pages: int,
        num_kv_heads: int,
        k_head_dim: int,
        v_head_dim: int,
        dtype: torch.dtype,
        device: torch.device,
    ) -> None:
        """Register the paged pool geometry (called by each backend at construction).

        Every served block on a server shares one pool of the same geometry; the pool tensors are
        allocated lazily on the runtime side (:meth:`use_paged_pool`). The first caller sets the
        config and the page budget; the rest must match.
        """
        config = dict(
            num_pages=num_pages,
            page_size=self.page_size,
            num_kv_heads=num_kv_heads,
            k_head_dim=k_head_dim,
            v_head_dim=v_head_dim,
            dtype=dtype,
            device=device,
        )
        if self._paged_pool_config is None:
            self._paged_pool_config = config
            self._num_pages.value = num_pages
        else:
            assert self._paged_pool_config == config, "all served blocks must share the paged pool geometry"

    @contextlib.asynccontextmanager
    async def allocate_paged_slots(self, num_slots: int, batch_size: int, timeout: Optional[float]):
        """Register ``num_slots`` paged cache slots (one per served block) for a new session.

        Reserves no pages up front; admission checks a snapshot for each slot's first token
        across the batch. Concurrent sessions may consume that capacity before the first write.
        Returns integer slot ids; freeing them returns their pages to the pool.
        """
        assert self.paged, "allocate_paged_slots requires paged mode"
        assert not self._is_runtime_context(), "must be called by a ConnectionHandler, not runtime"
        validate_paged_batch_size(batch_size, max_rows=self._num_pages.value)
        if self.max_alloc_timeout is not None:
            timeout = self.max_alloc_timeout if timeout is None else min(timeout, self.max_alloc_timeout)

        # Every slot needs at least one page per batch row for its first token.
        await self._wait_for_free_pages(num_slots * batch_size, timeout)
        with self._lock_metadata:
            slot_ids = [int(self.handle_counter) + i for i in range(num_slots)]
            self.handle_counter += num_slots
            self._pipe_send.send(("paged_register", slot_ids, batch_size))
        try:
            yield slot_ids
        finally:
            with self._lock_metadata:
                self._pipe_send.send(("paged_free", slot_ids, None))

    async def _wait_for_free_pages(self, num_pages: int, timeout: Optional[float]) -> None:
        if num_pages > self._num_pages.value:
            raise AllocationFailed("Paged cache cannot fit the requested batch across its slots")

        def has_room() -> bool:
            return self._num_pages.value - self._paged_used_pages.value >= num_pages

        if has_room():
            return
        if timeout == 0:
            raise AllocationFailed(f"Paged cache full: no room for {num_pages} initial pages")

        loop = asyncio.get_event_loop()
        deadline = None if timeout is None else time.perf_counter() + timeout
        while not has_room():
            remaining = None if deadline is None else deadline - time.perf_counter()
            if remaining is not None and remaining <= 0:
                raise AllocationFailed(f"Paged cache full: no room for {num_pages} initial pages in {timeout}s")
            await loop.run_in_executor(None, self._memory_freed_event.wait, remaining)
            self._memory_freed_event.clear()

    @contextlib.contextmanager
    def use_paged_pool(self):
        """Runtime-side access to the shared page pool; drains pending slot register/free requests.

        Mirrors :meth:`use_cache`: called only by the runtime (single thread, no process parallelism),
        it may run concurrently with connection handlers registering/freeing slots over the pipe.
        """
        assert os.getpid() == self.runtime_pid
        if self.runtime_thread_id is None:
            self.runtime_thread_id = threading.get_ident()
        assert self._is_runtime_context()

        pool = self._ensure_paged_pool()
        while self._pipe_recv.poll():
            tag, slot_ids, extra = self._pipe_recv.recv()
            if tag == "paged_register":
                for slot_id in slot_ids:
                    pool.register_slot(slot_id, extra)  # extra = batch_size
            elif tag == "paged_free":
                for slot_id in slot_ids:
                    pool.free_slot(slot_id)
                self._memory_freed_event.set()
            else:
                raise RuntimeError(f"Unexpected paged pipe message tag: {tag}")
        try:
            yield pool
        finally:
            self._paged_used_pages.value = pool.num_used_pages

    def _ensure_paged_pool(self) -> "PagedKVPool":
        if self._paged_pool is None:
            assert self._paged_pool_config is not None, "paged pool used before configure_paged_pool()"
            self._paged_pool = PagedKVPool(**self._paged_pool_config)
        return self._paged_pool


class AllocationFailed(Exception):
    pass


class PagedKVPool:
    """Runtime-side paged key/value store shared by every paged inference session on a server.

    One pair of pool tensors per device holds fixed-size *pages* (``page_size`` tokens each). Each
    logical *slot* -- one served block's cache for one session -- owns a **block table** mapping
    logical token positions to physical pages, grown lazily as the sequence extends, so a session
    only occupies pages for the tokens it actually generates (unlike the contiguous cache, which
    reserves ``max_length`` up front). :meth:`gather` materializes a contiguous BLOOM-layout prefix
    for the existing block/attention path (Stage 5a); a later kernel (Stage 5b) will read pages in
    place. Pages are stored per (slot, batch-row) with no cross-row sharing, so a beam-search
    ``hypo_ids`` reorder is an exact permutation with copy-on-duplicate.

    The pool is pure and synchronous: it lives entirely on the runtime thread/process (only the
    runtime allocates device tensors), so it needs no locks of its own. Single tensor-parallel
    shard only in 5a.
    """

    def __init__(
        self,
        *,
        num_pages: int,
        page_size: int,
        num_kv_heads: int,
        k_head_dim: int,
        v_head_dim: int,
        dtype: torch.dtype,
        device: torch.device,
    ):
        self.num_pages = num_pages
        self.page_size = page_size
        self.num_kv_heads = num_kv_heads
        self.k_head_dim = k_head_dim
        self.v_head_dim = v_head_dim
        self.dtype = dtype
        self.device = device
        # BLOOM layout per page: key [pages, kv_heads, k_head_dim, page_size],
        # value [pages, kv_heads, page_size, v_head_dim] -- so gather/scatter are contiguous copies.
        self.key_pool = torch.zeros((num_pages, num_kv_heads, k_head_dim, page_size), dtype=dtype, device=device)
        self.value_pool = torch.zeros((num_pages, num_kv_heads, page_size, v_head_dim), dtype=dtype, device=device)
        self._free_pages: List[int] = list(range(num_pages))
        self._block_tables: Dict[int, List[List[int]]] = {}  # slot_id -> per-row list of physical page indices

    @property
    def num_free_pages(self) -> int:
        return len(self._free_pages)

    @property
    def num_used_pages(self) -> int:
        return self.num_pages - len(self._free_pages)

    def register_slot(self, slot_id: int, batch_size: int) -> None:
        validate_paged_batch_size(batch_size, max_rows=self.num_pages)
        if slot_id in self._block_tables:
            return  # idempotent: a slot may be re-announced across steps
        self._block_tables[slot_id] = [[] for _ in range(batch_size)]

    def free_slot(self, slot_id: int) -> None:
        block_table = self._block_tables.pop(slot_id, None)
        if block_table is None:
            return
        for row_pages in block_table:
            self._free_pages.extend(row_pages)

    def _acquire_page(self) -> int:
        if not self._free_pages:
            raise AllocationFailed("Paged attention cache is full: no free pages left")
        return self._free_pages.pop()

    def gather(self, slot_id: int, prefix_length: int) -> Sequence[torch.Tensor]:
        """Materialize the first ``prefix_length`` tokens as contiguous BLOOM-layout key/value.

        Returns ``(key, value)`` matching ``StandardGQACache.select_layer_past``:
        ``key [batch*kv_heads, k_head_dim, prefix_length]``,
        ``value [batch*kv_heads, prefix_length, v_head_dim]``.
        """
        block_table = self._block_tables[slot_id]
        batch_size = len(block_table)
        if prefix_length == 0:
            key = self.key_pool.new_empty((batch_size * self.num_kv_heads, self.k_head_dim, 0))
            value = self.value_pool.new_empty((batch_size * self.num_kv_heads, 0, self.v_head_dim))
            return key, value

        num_logical_pages = math.ceil(prefix_length / self.page_size)
        idx = torch.tensor([row[:num_logical_pages] for row in block_table], device=self.device)  # [batch, npages]

        gathered_padded = num_logical_pages * self.page_size
        keys = self.key_pool[idx]  # [batch, npages, kv_heads, k_head_dim, page_size]
        keys = keys.permute(0, 2, 3, 1, 4).reshape(batch_size, self.num_kv_heads, self.k_head_dim, gathered_padded)
        keys = keys[:, :, :, :prefix_length].reshape(batch_size * self.num_kv_heads, self.k_head_dim, prefix_length)

        values = self.value_pool[idx]  # [batch, npages, kv_heads, page_size, v_head_dim]
        values = values.permute(0, 2, 1, 3, 4).reshape(batch_size, self.num_kv_heads, gathered_padded, self.v_head_dim)
        values = values[:, :, :prefix_length, :].reshape(batch_size * self.num_kv_heads, prefix_length, self.v_head_dim)
        return keys, values

    def scatter(self, slot_id: int, new_kvs: Sequence[torch.Tensor], prefix_length: int) -> None:
        """Write freshly computed keys/values for tokens ``[prefix_length:new_length]`` into pages.

        ``new_kvs`` is the block's full-length output in BLOOM layout (``key [b*kv, k_head_dim,
        new_length]``, ``value [b*kv, new_length, v_head_dim]``); only the tail past
        ``prefix_length`` is new and gets written, allocating pages at page boundaries.
        """
        block_table = self._block_tables[slot_id]
        batch_size = len(block_table)
        new_key, new_value = new_kvs
        new_length = new_key.shape[-1]
        if new_length <= prefix_length:
            return
        new_key = new_key.reshape(batch_size, self.num_kv_heads, self.k_head_dim, new_length)
        new_value = new_value.reshape(batch_size, self.num_kv_heads, new_length, self.v_head_dim)

        num_logical_pages = math.ceil(new_length / self.page_size)
        for row_pages in block_table:  # grow every row's block table to cover the new length
            while len(row_pages) < num_logical_pages:
                row_pages.append(self._acquire_page())

        first_page = prefix_length // self.page_size
        for lp in range(first_page, num_logical_pages):
            tok_start = max(prefix_length, lp * self.page_size)
            tok_end = min(new_length, (lp + 1) * self.page_size)
            off_start = tok_start - lp * self.page_size
            off_end = tok_end - lp * self.page_size
            pages_lp = torch.tensor([row[lp] for row in block_table], device=self.device)  # [batch]
            self.key_pool[pages_lp, :, :, off_start:off_end] = new_key[:, :, :, tok_start:tok_end]
            self.value_pool[pages_lp, :, off_start:off_end, :] = new_value[:, :, tok_start:tok_end, :]

    def _validate_hypo_ids(self, slot_id: int, hypo_ids: torch.Tensor, *, allow_empty: bool = False) -> List[int]:
        if slot_id not in self._block_tables:
            raise ValueError("Unknown paged cache slot")
        if hypo_ids.ndim != 1 or hypo_ids.dtype != torch.int64:
            raise ValueError("Paged beam indices must be a one-dimensional int64 tensor")
        batch_size = len(self._block_tables[slot_id])
        if allow_empty and hypo_ids.numel() == 0:
            return []
        if hypo_ids.numel() != batch_size:
            raise ValueError("Paged beam indices must preserve the allocated batch size")
        indices = hypo_ids.tolist()
        if any(index < 0 or index >= batch_size for index in indices):
            raise ValueError("Paged beam index is outside the allocated batch")
        return indices

    def validate_inference_step(self, slot_id: int, batch_size: int, hypo_ids: torch.Tensor) -> None:
        """Check decoded step dimensions against this slot before any cache read or mutation."""
        if slot_id not in self._block_tables:
            raise ValueError("Unknown paged cache slot")
        if batch_size != len(self._block_tables[slot_id]):
            raise ValueError("Paged inference must preserve the allocated batch size")
        self._validate_hypo_ids(slot_id, hypo_ids, allow_empty=True)

    def reorder(self, slot_id: int, hypo_ids: torch.Tensor) -> None:
        """Reorder a fixed batch atomically, copying pages for duplicated beam sources.

        The pool is confined to one synchronous runtime thread. Copies may overwrite free
        pages, but neither their ownership nor the live slot changes until every copy succeeds.
        Duplicate pages require temporary free capacity before unreferenced rows are reclaimed.
        """
        indices = self._validate_hypo_ids(slot_id, hypo_ids)
        block_table = self._block_tables[slot_id]
        claimed = set()
        pages_needed = 0
        for src in indices:
            if src in claimed:
                pages_needed += len(block_table[src])
            claimed.add(src)
        if pages_needed > len(self._free_pages):
            raise AllocationFailed("Paged attention cache lacks temporary pages for beam copies")

        # Stage every ownership change locally. A copy/allocation exception leaves the old
        # table and free list intact, including when some free-page contents were overwritten.
        available_pages = iter(reversed(self._free_pages))
        free_pages = self._free_pages[:-pages_needed] if pages_needed else list(self._free_pages)
        result: List[List[int]] = []
        claimed = set()
        for src in indices:
            if src not in claimed:
                claimed.add(src)
                result.append(block_table[src])  # first use reuses the source pages in place
            else:
                copied = []
                for page in block_table[src]:
                    new_page = next(available_pages)
                    self.key_pool[new_page].copy_(self.key_pool[page])
                    self.value_pool[new_page].copy_(self.value_pool[page])
                    copied.append(new_page)
                result.append(copied)
        for src, row_pages in enumerate(block_table):  # reclaim rows no dest row referenced
            if src not in claimed:
                free_pages.extend(row_pages)
        self._block_tables[slot_id] = result
        self._free_pages = free_pages
