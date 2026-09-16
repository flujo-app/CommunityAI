"""Offline regression tests for the boundary before paged cache registration.

Execute the actual source methods with small transport/cache doubles so these
security checks run with stdlib alone, without a model, GPU, Hivemind or pytest.
This does not substitute for the real transport/cache integration suite.
"""

import ast
import asyncio
import contextlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class AdmissionRejected(RuntimeError):
    pass


@contextlib.asynccontextmanager
async def _test_timeout(_seconds):
    # Timeout behavior belongs to the existing transport tests, not this fixture.
    yield


def _load_methods():
    namespace = {
        "asyncio": asyncio,
        "contextlib": contextlib,
        "math": math,
        "timeout": _test_timeout,
        "anext": anext,
        "AdmissionRejected": AdmissionRejected,
        "MSGPackSerializer": SimpleNamespace(loads=json.loads),
    }
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    cache_tree = ast.parse((ROOT / "src/drift/server/memory_cache.py").read_text(encoding="utf-8"))
    for node in cache_tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "MAX_PAGED_BATCH_SIZE" for target in node.targets
        ):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "validate_paged_batch_size":
            nodes.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "AllocationFailed":
            nodes.append(node)
        elif isinstance(node, ast.ClassDef) and node.name in {"MemoryCache", "PagedKVPool"}:
            methods = {"allocate_paged_slots", "_wait_for_free_pages", "register_slot"}
            nodes.append(ast.ClassDef(
                name=node.name, bases=[], keywords=[], decorator_list=[],
                body=[item for item in node.body if getattr(item, "name", None) in methods],
            ))
    handler_tree = ast.parse((ROOT / "src/drift/server/handler.py").read_text(encoding="utf-8"))
    handler = next(node for node in handler_tree.body if isinstance(node, ast.ClassDef)
                   and node.name == "TransformerConnectionHandler")
    nodes.append(ast.ClassDef(
        name=handler.name, bases=[], keywords=[], decorator_list=[],
        body=[item for item in handler.body
              if getattr(item, "name", None) in {"rpc_inference", "_inference_batch_size"}],
    ))
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, "<actual cache/handler source methods>", "exec"), namespace)
    return namespace


SOURCE = _load_methods()


def _load_real_pool_and_paged_backend(torch):
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for filename, class_name, methods in (
        ("memory_cache.py", "PagedKVPool", None),
        ("backend.py", "TransformerBackend", {"_paged_inference_step"}),
    ):
        tree = ast.parse((ROOT / "src/drift/server" / filename).read_text(encoding="utf-8"))
        node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == class_name)
        if methods is not None:
            node = ast.ClassDef(name=node.name, bases=[], keywords=[], decorator_list=[],
                                body=[item for item in node.body if getattr(item, "name", None) in methods])
        nodes.append(node)
    namespace = dict(SOURCE, torch=torch, is_dummy=lambda tensor: tensor.numel() == 0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<actual pool and paged backend source>", "exec"), namespace)
    return namespace


class PagedReorderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import torch
        except ImportError:
            raise unittest.SkipTest("torch unavailable; use the existing product interpreter")
        cls.torch = torch
        cls.source = _load_real_pool_and_paged_backend(torch)

    def make_pool(self, *, pages=8, batch=2):
        pool = self.source["PagedKVPool"](
            num_pages=pages, page_size=1, num_kv_heads=1, k_head_dim=1, v_head_dim=1,
            dtype=self.torch.float32, device=self.torch.device("cpu"),
        )
        pool.register_slot(0, batch)
        values = self.torch.arange(batch, dtype=self.torch.float32).reshape(batch, 1, 1)
        pool.scatter(0, (values, values + 10), 0)
        return pool

    def state(self, pool):
        return ([list(row) for row in pool._block_tables[0]], list(pool._free_pages),
                tuple(tensor.clone() for tensor in pool.gather(0, 1)))

    def assert_state_equal(self, pool, before):
        table, free, kvs = before
        self.assertEqual(pool._block_tables[0], table)
        self.assertEqual(pool._free_pages, free)
        for actual, expected in zip(pool.gather(0, 1), kvs):
            self.assertTrue(self.torch.equal(actual, expected))

    def test_original_oversized_beam_repro_rejects_without_leaking(self):
        pool = self.make_pool(batch=1)
        before = self.state(pool)
        with self.assertRaises(ValueError):
            pool.reorder(0, self.torch.tensor([0] * 9))
        self.assert_state_equal(pool, before)
        pool.free_slot(0)
        self.assertEqual(pool.num_free_pages, 8)

    def test_invalid_beam_rank_dtype_count_and_indices_are_atomic(self):
        torch = self.torch
        for ids in (torch.tensor(0), torch.tensor([[0, 1]]), torch.tensor([0., 1.]),
                    torch.tensor([False, True]), torch.tensor([0, 1], dtype=torch.int32),
                    torch.tensor([], dtype=torch.int64), torch.tensor([0]), torch.tensor([0, 1, 1]),
                    torch.tensor([-1, 0]), torch.tensor([0, 2]), torch.tensor([0, 2**62])):
            with self.subTest(shape=tuple(ids.shape), dtype=str(ids.dtype), ids=ids.tolist()):
                pool = self.make_pool()
                before = self.state(pool)
                with self.assertRaises(ValueError):
                    pool.reorder(0, ids)
                self.assert_state_equal(pool, before)
                pool.free_slot(0)
                self.assertEqual(pool.num_free_pages, 8)

    def test_valid_count_but_insufficient_duplicate_capacity_is_atomic(self):
        # Four live rows, only two free pages, but [0,0,0,0] needs three staging pages.
        pool = self.make_pool(pages=6, batch=4)
        before = self.state(pool)
        with self.assertRaises(SOURCE["AllocationFailed"]):
            pool.reorder(0, self.torch.tensor([0, 0, 0, 0]))
        self.assert_state_equal(pool, before)
        pool.free_slot(0)
        self.assertEqual(pool.num_free_pages, 6)

    def test_copy_failure_does_not_claim_staging_pages_or_change_slot(self):
        pool = self.make_pool(batch=3)
        before = self.state(pool)
        real_value_pool = pool.value_pool
        copies = []

        class Page:
            def __init__(self, tensor):
                self.tensor = tensor

            def copy_(self, source):
                copies.append(1)
                if len(copies) == 2:
                    raise RuntimeError("injected second-copy failure")
                self.tensor.copy_(source.tensor)

        class FailingCopies:
            def __getitem__(self, index):
                return Page(real_value_pool[index])

        with mock.patch.object(pool, "value_pool", FailingCopies()):
            with self.assertRaisesRegex(RuntimeError, "injected second-copy failure"):
                pool.reorder(0, self.torch.tensor([0, 0, 0]))
        self.assertEqual(len(copies), 2)
        self.assert_state_equal(pool, before)
        pool.free_slot(0)
        self.assertEqual(pool.num_free_pages, 8)

    def test_valid_duplicate_beams_preserve_values_and_free_every_page(self):
        pool = self.make_pool(batch=3)
        before = self.state(pool)
        ids = self.torch.tensor([2, 0, 2])
        pool.reorder(0, ids)
        for actual, expected in zip(pool.gather(0, 1), before[2]):
            self.assertTrue(self.torch.equal(actual, expected[ids]))
        owned = [page for row in pool._block_tables[0] for page in row]
        self.assertEqual(len(owned), len(set(owned)))
        self.assertEqual(pool.num_used_pages, 3)
        pool.free_slot(0)
        self.assertEqual(pool.num_free_pages, 8)

    def test_unknown_slot_is_rejected_without_changing_other_slots(self):
        pool = self.make_pool()
        before = self.state(pool)
        with self.assertRaises(ValueError):
            pool.reorder(99, self.torch.tensor([0, 1]))
        self.assert_state_equal(pool, before)

    def backend_step(self, pool, hidden_batch, ids):
        backend = self.source["TransformerBackend"]()
        backend.memory_cache = SimpleNamespace(use_paged_pool=lambda: contextlib.nullcontext(pool))
        backend._estimate_max_chunk_length = lambda *args: 1
        backend._forward_chunked = lambda hidden, past, chunk: (hidden, past)
        return backend._paged_inference_step(
            self.torch.zeros(hidden_batch, 1, 1), ids,
            SimpleNamespace(cache_handles=(0,), prefix_length=1),
        )

    def test_backend_rejects_batch_mismatch_and_invalid_dummy_before_cache_access(self):
        torch = self.torch
        for batch, ids in ((1, torch.tensor([0, 1])), (1, torch.empty(0, dtype=torch.int64)),
                           (2, torch.empty((1, 0), dtype=torch.int64)), (2, torch.empty(0))):
            with self.subTest(batch=batch, shape=tuple(ids.shape), dtype=str(ids.dtype)):
                pool = self.make_pool()
                before = self.state(pool)
                with mock.patch.object(pool, "gather", side_effect=AssertionError("unexpected cache access")):
                    with self.assertRaises(ValueError):
                        self.backend_step(pool, batch, ids)
                self.assert_state_equal(pool, before)

    def test_backend_preserves_valid_dummy_permutation_and_duplicate_steps(self):
        for ids in (self.torch.empty(0, dtype=self.torch.int64), self.torch.tensor([1, 0]),
                    self.torch.tensor([1, 1])):
            with self.subTest(ids=ids.tolist()):
                pool = self.make_pool()
                before = self.state(pool)
                outputs = self.backend_step(pool, 2, ids)
                self.assertEqual(tuple(outputs[0].shape), (2, 1, 1))
                expected_ids = ids if ids.numel() else self.torch.tensor([0, 1])
                for actual, expected in zip(pool.gather(0, 1), before[2]):
                    self.assertTrue(self.torch.equal(actual, expected[expected_ids]))
                pool.free_slot(0)
                self.assertEqual(pool.num_free_pages, 8)


class PagedAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def make_cache(self, pages=16):
        cache = SOURCE["MemoryCache"]()
        cache.paged = True
        cache.max_alloc_timeout = None
        cache._is_runtime_context = lambda: False
        cache._num_pages = SimpleNamespace(value=pages)
        cache._paged_used_pages = SimpleNamespace(value=0)
        cache._lock_metadata = contextlib.nullcontext()
        cache.handle_counter = 0
        messages = []
        cache._pipe_send = SimpleNamespace(send=messages.append)
        return cache, messages

    async def test_invalid_batch_never_reaches_registration_pipe(self):
        for batch in (True, 0, -1, 1.5, "2", 8193, 10000):
            with self.subTest(batch=batch):
                cache, messages = self.make_cache(pages=20000)
                with self.assertRaises(ValueError):
                    async with cache.allocate_paged_slots(1, batch, timeout=0):
                        pass
                self.assertEqual(messages, [])
                self.assertEqual(cache.handle_counter, 0)

    async def test_batch_cannot_exceed_pool_rows(self):
        cache, messages = self.make_cache(pages=3)
        with self.assertRaises(ValueError):
            async with cache.allocate_paged_slots(1, 4, timeout=0):
                pass
        self.assertEqual(messages, [])

    async def test_page_admission_accounts_for_every_batch_row(self):
        cache, messages = self.make_cache(pages=3)
        with self.assertRaises(SOURCE["AllocationFailed"]):
            async with cache.allocate_paged_slots(2, 2, timeout=0):
                pass
        self.assertEqual(messages, [])

    async def test_impossible_initial_page_count_rejects_without_waiting(self):
        cache, messages = self.make_cache(pages=3)
        with self.assertRaises(SOURCE["AllocationFailed"]):
            async with cache.allocate_paged_slots(2, 2, timeout=None):
                pass
        self.assertEqual(messages, [])

    async def test_valid_batch_registers_and_frees(self):
        cache, messages = self.make_cache(pages=6)
        async with cache.allocate_paged_slots(2, 3, timeout=0) as slots:
            self.assertEqual(slots, [0, 1])
            self.assertEqual(messages, [("paged_register", [0, 1], 3)])
        self.assertEqual(messages[-1], ("paged_free", [0, 1], None))

    def test_runtime_rejects_invalid_rows_without_creating_a_table(self):
        for batch in (True, 0, -1, 1.5, "2", 8193, 10000):
            with self.subTest(batch=batch):
                pool = SOURCE["PagedKVPool"]()
                pool.num_pages = 20000
                pool._block_tables = {}
                with self.assertRaises(ValueError):
                    pool.register_slot(0, batch)
                self.assertEqual(pool._block_tables, {})

    def test_runtime_enforces_pool_capacity_and_preserves_idempotence(self):
        pool = SOURCE["PagedKVPool"]()
        pool.num_pages = 3
        pool._block_tables = {}
        with self.assertRaises(ValueError):
            pool.register_slot(0, 4)
        pool.register_slot(0, 3)
        original = pool._block_tables[0]
        pool.register_slot(0, 3)
        self.assertIs(pool._block_tables[0], original)
        self.assertEqual(len(original), 3)

    def test_runtime_accepts_hard_limit_when_pool_has_capacity(self):
        pool = SOURCE["PagedKVPool"]()
        pool.num_pages = 8192
        pool._block_tables = {}
        pool.register_slot(0, 8192)
        self.assertEqual(len(pool._block_tables[0]), 8192)

    def test_real_cpu_page_pool_roundtrip_when_torch_available(self):
        try:
            import torch
        except ImportError:
            self.skipTest("torch is unavailable; run this test with the existing product interpreter")
        tree = ast.parse((ROOT / "src/drift/server/memory_cache.py").read_text(encoding="utf-8"))
        pool_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PagedKVPool")
        future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
        module = ast.fix_missing_locations(ast.Module(body=[future, pool_class], type_ignores=[]))
        namespace = dict(SOURCE, torch=torch)
        exec(compile(module, "<actual full PagedKVPool source>", "exec"), namespace)
        pool = namespace["PagedKVPool"](
            num_pages=12, page_size=2, num_kv_heads=1, k_head_dim=2, v_head_dim=2,
            dtype=torch.float32, device=torch.device("cpu"),
        )
        with self.assertRaises(ValueError):
            pool.register_slot(0, 10000)
        self.assertEqual(pool._block_tables, {})
        pool.register_slot(0, 2)
        key = torch.arange(12, dtype=torch.float32).reshape(2, 2, 3)
        value = torch.arange(12, dtype=torch.float32).reshape(2, 3, 2)
        pool.scatter(0, (key, value), 0)
        actual_key, actual_value = pool.gather(0, 3)
        self.assertTrue(torch.equal(actual_key, key))
        self.assertTrue(torch.equal(actual_value, value))
        pool.reorder(0, torch.tensor([1, 0]))
        reordered_key, reordered_value = pool.gather(0, 3)
        self.assertTrue(torch.equal(reordered_key, key[[1, 0]]))
        self.assertTrue(torch.equal(reordered_value, value[[1, 0]]))
        pool.free_slot(0)
        self.assertEqual(pool.num_free_pages, 12)

    async def test_forged_rpc_shapes_reject_before_any_cache_allocation(self):
        for shape in ((), (1,), (1, 2), (1, 2, 4, 1), (0, 1, 4), (-1, 1, 4),
                      (10000, 0, 4), (1, -1, 4), (1, 9, 4), (1, 1, 5), (3, 2, 4)):
            with self.subTest(shape=shape):
                events = await self.drive_request(shape, rejected=True)
                self.assertEqual(events, ["acquire", "release"])

    async def test_handler_paged_hard_cap_applies_even_with_larger_task_limit(self):
        events = await self.drive_request((10000, 0, 4), rejected=True, max_tokens=20000)
        self.assertEqual(events, ["acquire", "release"])

    async def test_valid_rpc_shapes_and_empty_step_preserve_allocation(self):
        for shape in ((1, 1, 4), (2, 2, 4), (2, 0, 4), None):
            with self.subTest(shape=shape):
                events = await self.drive_request(shape, rejected=False)
                batch = 1 if shape is None else shape[0]
                self.assertEqual(events, ["acquire", ("allocate", batch), "free", "release"])

    async def drive_request(self, shape, *, rejected, max_tokens=4):
        events = []
        handler = SOURCE["TransformerConnectionHandler"]()
        handler.session_timeout = handler.step_timeout = 2
        handler.inference_max_length = 8
        lease = SimpleNamespace(release=lambda: events.append("release"))

        def acquire(peer):
            events.append("acquire")
            return lease

        handler._admission_state = SimpleNamespace(acquire=acquire)
        handler._require_bounded_inference_request = lambda request: None
        handler._check_uids = lambda uid: (uid,)
        handler._log_request = lambda *args, **kwargs: None
        handler._check_manifest_digest = lambda metadata: None
        handler.module_backends = {"model.0": SimpleNamespace(
            config=SimpleNamespace(hidden_size=4), inference_pool=SimpleNamespace(max_batch_size=max_tokens),
            memory_cache=SimpleNamespace(paged=True),
        )}
        handler._get_active_adapter = lambda metadata: ""
        handler._iterate_inference_steps = lambda *args: None
        handler._prioritizer = handler.quant_type = None

        @contextlib.asynccontextmanager
        async def allocate(*args, batch_size, **kwargs):
            events.append(("allocate", batch_size))
            try:
                yield []
            finally:
                events.append("free")

        handler._allocate_cache = allocate

        async def no_compute(**kwargs):
            if False:
                yield None

        SOURCE["iterate_rpc_inference"] = no_compute

        async def requests():
            yield SimpleNamespace(
                uid="model.0", metadata=json.dumps({"max_length": 8}),
                tensors=[] if shape is None else [SimpleNamespace(size=shape)],
            )

        iterator = handler.rpc_inference(requests(), SimpleNamespace(remote_id="fixture-peer"))
        if rejected:
            with self.assertRaises(AdmissionRejected):
                await anext(iterator)
        else:
            with self.assertRaises(StopAsyncIteration):
                await anext(iterator)
        await iterator.aclose()
        return events


if __name__ == "__main__":
    unittest.main()
