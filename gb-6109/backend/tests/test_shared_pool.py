"""共享复演页池与确定性重演的单元测试，仅依赖标准库。

运行：在 backend 目录执行 ``python3 -m unittest discover -s tests -v``。
"""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from src.domain import scenario
from src.domain.fixtures import FIXTURES
from src.domain.pool import SharedPagePool
from src.domain.pool_scenario import normalize_pool, run_shared


class SharedPagePoolTest(unittest.TestCase):

    def test_grant_by_batch_peak_and_invariant(self):
        pool = SharedPagePool(5)
        self.assertTrue(pool.request("A", 3)[0])
        self.assertTrue(pool.request("B", 2)[0])
        snapshot = pool.snapshot()
        self.assertEqual(snapshot.reserved, 5)
        self.assertEqual(snapshot.available, 0)
        self.assertLessEqual(snapshot.reserved, snapshot.capacity)
        self.assertEqual(pool.held_pages("A"), 3)

    def test_peak_over_capacity_is_rejected_without_holding(self):
        pool = SharedPagePool(5)
        granted, reason = pool.request("X", 6)
        self.assertFalse(granted)
        self.assertIn("超过共享页池总额", reason)
        self.assertEqual(pool.snapshot().reserved, 0)
        self.assertIn("X", pool.snapshot().rejected)

    def test_concurrent_reservation_never_exceeds_capacity(self):
        capacity = 8
        pool = SharedPagePool(capacity)
        peaks = [3, 2, 4, 1, 5, 2, 3, 1, 4, 2]
        barrier = threading.Barrier(len(peaks))

        def worker(index, peak):
            barrier.wait()
            pool.acquire(f"B{index}", peak)
            snapshot = pool.snapshot()
            self.assertLessEqual(snapshot.reserved, capacity)
            pool.release(f"B{index}")

        with ThreadPoolExecutor(max_workers=len(peaks)) as executor:
            list(executor.map(worker, range(len(peaks)), peaks))
        self.assertEqual(pool.snapshot().reserved, 0)
        self.assertEqual(len(pool.snapshot().finished), len(peaks))

    def test_randomized_concurrent_invariant_holds(self):
        import random

        capacity = 10
        pool = SharedPagePool(capacity, max_overtakes=3)
        rng = random.Random(20261126)
        stop = threading.Event()

        def churner(prefix):
            count = 0
            while not stop.is_set() and count < 150:
                batch_id = f"{prefix}-{count}"
                peak = rng.randint(1, capacity)
                if pool.acquire(batch_id, peak, timeout=2):
                    if pool.snapshot().reserved > capacity:
                        raise AssertionError("共享页池超占")
                    pool.release(batch_id)
                count += 1

        threads = [threading.Thread(target=churner, args=(f"T{i}",)) for i in range(6)]
        for thread in threads:
            thread.start()
        stop.wait(4)
        stop.set()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        for batch_id in list(pool.snapshot().held):
            pool.release(batch_id)
        self.assertEqual(pool.snapshot().reserved, 0)

    def test_small_batch_overtakes_then_barrier_blocks_starvation(self):
        # 5 页池：先占 4 页，留 1 页给小单；大单（4 页）排队。
        pool = SharedPagePool(5, max_overtakes=2)
        pool.request("holder", 4)
        granted, _ = pool.request("big", 4)
        self.assertFalse(granted)
        # 两个 1 页小单够用，越过大单先跑；每放行一个，大单让行计数加一。
        self.assertTrue(pool.request("small-1", 1)[0])
        self.assertEqual(pool.snapshot().waiting[0]["overtaken"], 1)
        self.assertEqual(pool.overtakes, 1)
        pool.release("small-1")
        self.assertTrue(pool.request("small-2", 1)[0])
        self.assertEqual(pool.snapshot().waiting[0]["overtaken"], 2)
        self.assertEqual(pool.overtakes, 2)
        # 第三个小单即使 1 页够用，也被队列屏障挡住。
        pool.release("small-2")
        self.assertFalse(pool.request("small-3", 1)[0])
        self.assertEqual(pool.held_pages("small-3"), 0)
        # holder 归还后大单立刻放行，小单随后。
        pool.release("holder")
        self.assertEqual(pool.held_pages("big"), 4)
        pool.pump()
        self.assertGreaterEqual(pool.held_pages("small-3"), 1)

    def test_blocking_acquire_wakes_on_release(self):
        pool = SharedPagePool(3)
        pool.request("first", 2)

        def later():
            self.assertTrue(pool.acquire("second", 2, timeout=5))

        thread = threading.Thread(target=later)
        thread.start()
        thread.join(1)
        self.assertTrue(thread.is_alive())
        pool.release("first")
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(pool.held_pages("second"), 2)

    def test_release_happens_once(self):
        pool = SharedPagePool(4)
        pool.request("A", 3)
        self.assertEqual(pool.release("A"), 3)
        self.assertEqual(pool.release("A"), 0)
        self.assertEqual(pool.snapshot().reserved, 0)
        self.assertEqual(pool.snapshot().finished, ["A"])

    def test_interrupted_batch_keeps_pages_until_single_return(self):
        pool = SharedPagePool(4)
        pool.request("cut", 3)
        # 断电回收前：暂占页始终计入总额。
        self.assertEqual(pool.snapshot().reserved, 3)
        self.assertEqual(pool.held_pages("cut"), 3)
        # 续跑不重新领页，额度沿用。
        self.assertEqual(pool.held_pages("cut"), 3)
        self.assertEqual(pool.release("cut"), 3)


class SharedPoolScenarioTest(unittest.TestCase):

    def setUp(self):
        self.payload = next(
            fx["payload"] for fx in FIXTURES if fx["code"] == "BR-POOL-1124"
        )

    def test_normalize_rejects_bad_batches(self):
        with self.assertRaises(ValueError):
            normalize_pool({"mode": "shared_pool", "batches": []})
        with self.assertRaises(ValueError):
            normalize_pool({"mode": "shared_pool",
                            "batches": [{"id": "x", "peak": 0, "duration": 1}]})
        with self.assertRaises(ValueError):
            normalize_pool({"mode": "shared_pool",
                            "batches": [{"id": "x", "peak": 2, "duration": 1,
                                         "interrupt": {"after": 5, "resume_after": 1}}]})

    def test_dispatch_through_scenario_run(self):
        result = scenario.run(self.payload, 5, None)
        self.assertEqual(result["kind"], "shared_pool")
        self.assertEqual(result["metrics"]["共享页总额"], 5)
        self.assertLessEqual(result["metrics"]["峰值并发占用"], 5)

    def test_replay_is_deterministic(self):
        first = run_shared(self.payload, 5)
        second = run_shared(self.payload, 5)
        self.assertEqual(first["rows"], second["rows"])
        self.assertEqual(first["batches"], second["batches"])

    def test_over_quota_batch_rejected_and_never_runs(self):
        result = run_shared(self.payload, 5)
        rejected = next(b for b in result["batches"] if b["状态"] == "额度退回")
        self.assertEqual(rejected["批次"], "B-超额-05")
        self.assertEqual(rejected["领页时刻"], "—")
        self.assertTrue(any(e["事件"] == "退回" for e in result["rows"]))

    def test_interrupted_batch_resumes_with_same_quota_and_returns_once(self):
        result = run_shared(self.payload, 5)
        cut = next(b for b in result["batches"] if b["批次"] == "B-断电-06")
        self.assertIn("断电", cut["断电中断"])
        events = result["rows"]
        grants = [e for e in events if e["批次"] == "B-断电-06" and e["事件"] == "领页"]
        releases = [e for e in events if e["批次"] == "B-断电-06" and e["事件"] == "归还"]
        self.assertEqual(len(grants), 1)
        self.assertEqual(len(releases), 1)
        self.assertTrue(any(e["事件"] == "续跑" and "沿用原预留" in e["说明"]
                            for e in events if e["批次"] == "B-断电-06"))

    def test_barrier_fixture_overtakes_then_prefers_big_batch(self):
        payload = next(fx["payload"] for fx in FIXTURES if fx["code"] == "BR-POOL-1126")
        result = run_shared(payload, 5)
        self.assertEqual(result["metrics"]["让行次数"], 2)
        big = next(b for b in result["batches"] if b["批次"] == "B-大单")
        third = next(b for b in result["batches"] if b["批次"] == "B-小单丙")
        self.assertIn("队列屏障", big["等待原因"])
        self.assertLessEqual(big["领页时刻"], third["领页时刻"])
        self.assertTrue(any("队列屏障" in e["说明"] for e in result["rows"]))

    def test_legacy_shadow_pager_scenario_still_works(self):
        payload = next(fx["payload"] for fx in FIXTURES if fx["code"] == "BR-K7-1124")
        result = scenario.run(payload, 5, None)
        self.assertNotIn("kind", result)
        self.assertIn("metrics", result)
        self.assertTrue(result["rows"])


if __name__ == "__main__":
    unittest.main()
