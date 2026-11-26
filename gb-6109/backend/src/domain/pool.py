"""车间共享复演页池。

多张配方签发单（批次）共用同一份物理页额，各自跑各自的配方。

约定：
- 领页按整批峰值一次性预留，批次运行期间一直占用，做完才整笔归还；
- 断电回收前暂占的页仍然占用额度，续跑沿用同一笔预留，不重复领取；
- 并发领页时总占额绝不超过页池上限，超过总额的批次直接退回；
- 页不够时批次进入等待队列；排在后面、额度够用的小单可以先跑，
  但每个等待者被插队达到上限后就成为队列屏障，杜绝大单一直被插队。
"""

from dataclasses import dataclass, field
from threading import Condition, RLock


@dataclass
class _Waiter:
    seq: int
    batch_id: str
    peak: int
    overtaken: int = 0
    granted: bool = False
    last_reason: str = field(default="", repr=False)


@dataclass
class PoolSnapshot:
    capacity: int
    reserved: int
    available: int
    held: dict
    waiting: list
    finished: list
    rejected: list
    overtakes: int


class SharedPagePool:
    """线程安全的共享页池，所有状态变更都在同一把锁内完成。"""

    def __init__(self, capacity, max_overtakes=2):
        if not isinstance(capacity, int) or capacity < 1:
            raise ValueError("页池总额须为正整数")
        self.capacity = capacity
        self.max_overtakes = max_overtakes
        self._reserved = 0
        self._held = {}
        self._finished = []
        self._rejected = []
        self._queue = []
        self._counter = 0
        self._overtakes = 0
        self._events = []
        self._lock = RLock()
        self._cond = Condition(self._lock)

    # ---- 事件与快照 -------------------------------------------------

    def _log(self, tick, batch_id, event, detail):
        self._events.append({
            "时刻": None if tick is None else int(tick),
            "批次": batch_id,
            "事件": event,
            "说明": detail,
            "已占页": self._reserved,
            "等待数": len(self._queue),
        })

    def log(self, tick, batch_id, event, detail):
        """驱动器追加批次生命周期事件，保证全场景只有一条有序事件流。"""
        with self._lock:
            self._log(tick, batch_id, event, detail)

    def events(self):
        with self._lock:
            return [dict(item) for item in self._events]

    def snapshot(self):
        with self._lock:
            return PoolSnapshot(
                capacity=self.capacity,
                reserved=self._reserved,
                available=self.capacity - self._reserved,
                held=dict(self._held),
                waiting=[dict(seq=w.seq, batch_id=w.batch_id, peak=w.peak,
                              overtaken=w.overtaken) for w in self._queue],
                finished=list(self._finished),
                rejected=list(self._rejected),
                overtakes=self._overtakes,
            )

    # ---- 公平调度 ---------------------------------------------------

    def _wait_reason(self, waiter):
        free = self.capacity - self._reserved
        if waiter.overtaken >= self.max_overtakes:
            return (f"需 {waiter.peak} 页、仅剩 {free} 页；已被后续小单越过 "
                    f"{waiter.overtaken} 次，成为队列屏障，后续批次一律在其后等待")
        if waiter.overtaken == 0:
            return f"需 {waiter.peak} 页、仅剩 {free} 页，在 FIFO 队列等待其它批次归还"
        return (f"需 {waiter.peak} 页、仅剩 {free} 页；已让行 {waiter.overtaken} 次，"
                f"达到 {self.max_overtakes} 次前够用的小单可先跑")

    def _pump_locked(self, tick):
        """沿 FIFO 队列尽量放行；队头让行达上限后形成屏障。

        让行计数只记在队头等待者身上：每越过它放行一个后面的小单，它的
        ``overtaken`` 加一。计数达到上限后它成为屏障，队列整体卡住，
        直到它领到页为止——小单能先跑，但大单不会被无限插队。
        等待原因只在原因文本变化时记一条，避免同拍或节拍间刷屏。
        """
        moved = False
        blocked = False
        for waiter in self._queue:
            if waiter.granted:
                continue
            free = self.capacity - self._reserved
            if free >= waiter.peak:
                # 动态取当前队头：大单被放行后，它后面的小单不再算越过它。
                head = next((w for w in self._queue if not w.granted), None)
                self._reserved += waiter.peak
                self._held[waiter.batch_id] = waiter.peak
                waiter.granted = True
                waiter.last_reason = ""
                if waiter is not head and head is not None:
                    # 越过队头放行：队头的让行（被越过）计数加一。
                    head.overtaken += 1
                    self._overtakes += 1
                self._log(tick, waiter.batch_id, "领页",
                          f"整批峰值 {waiter.peak} 页已预留，池内剩余 "
                          f"{self.capacity - self._reserved} 页")
                moved = True
                continue
            reason = self._wait_reason(waiter)
            if reason != waiter.last_reason:
                waiter.last_reason = reason
                self._log(tick, waiter.batch_id, "等待", reason)
            head = next((w for w in self._queue if not w.granted), None)
            if waiter is head and waiter.overtaken >= self.max_overtakes:
                # 队头屏障：即使后面的小单够用也不许越过。
                blocked = True
                break
        if moved:
            self._queue = [w for w in self._queue if not w.granted]
            self._cond.notify_all()
        return moved or blocked

    # ---- 对外操作 ---------------------------------------------------

    def request(self, batch_id, peak, tick=None):
        """非阻塞领页：够就立即预留，不够就入队等待，超额直接退回。

        返回 (granted, reason)。granted 为 True 时 reason 为 None；
        为 False 时批次已在队列中，reason 说明等待原因。
        """
        if not isinstance(peak, int) or peak < 1:
            raise ValueError("批次峰值须为正整数")
        with self._lock:
            if peak > self.capacity:
                self._rejected.append(batch_id)
                reason = (f"单批峰值 {peak} 页超过共享页池总额 {self.capacity} 页，"
                          "整单退回，不占页额")
                self._log(tick, batch_id, "退回", reason)
                return False, reason
            self._counter += 1
            waiter = _Waiter(seq=self._counter, batch_id=batch_id, peak=peak)
            self._queue.append(waiter)
            self._log(tick, batch_id, "排队",
                      f"按整批峰值申领 {peak} 页，当前已占 {self._reserved}/"
                      f"{self.capacity} 页，进入 FIFO 队列第 {len(self._queue)} 位")
            self._pump_locked(tick)
            if waiter.granted:
                return True, None
            reason = (f"页额不足：需 {peak} 页，池内仅剩 {self.capacity - self._reserved} 页；"
                      f"排在 FIFO 队列中等待其它批次归还")
            return False, reason

    def acquire(self, batch_id, peak, tick=None, timeout=None):
        """阻塞领页，供真实并发线程使用；超时未领到返回 False。

        与 request 走同一套公平调度，只是在条件变量上等待 pump 放行。
        """
        import time

        if not isinstance(peak, int) or peak < 1:
            raise ValueError("批次峰值须为正整数")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            if peak > self.capacity:
                self._rejected.append(batch_id)
                self._log(tick, batch_id, "退回",
                          f"单批峰值 {peak} 页超过共享页池总额 {self.capacity} 页，整单退回")
                return False
            self._counter += 1
            waiter = _Waiter(seq=self._counter, batch_id=batch_id, peak=peak)
            self._queue.append(waiter)
            self._log(tick, batch_id, "排队",
                      f"按整批峰值申领 {peak} 页，当前已占 {self._reserved}/"
                      f"{self.capacity} 页，进入 FIFO 队列第 {len(self._queue)} 位")
            while not waiter.granted:
                self._pump_locked(tick)
                if waiter.granted:
                    break
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    self._queue.remove(waiter)
                    self._log(tick, batch_id, "等待超时", "在限定时间内未等到足够页额，撤出队列")
                    self._pump_locked(tick)
                    return False
                self._cond.wait(timeout=remaining)
            return True

    def pump(self, tick=None):
        """归还等事件后重新驱动公平调度（重演驱动器按节拍调用）。"""
        with self._lock:
            return self._pump_locked(tick)

    def release(self, batch_id, tick=None):
        """批次做完后整笔归还，一个批次只还一次。"""
        with self._lock:
            pages = self._held.pop(batch_id, None)
            if pages is None:
                self._log(tick, batch_id, "归还忽略", "该批次无有效预留，无需归还")
                return 0
            self._reserved -= pages
            self._finished.append(batch_id)
            self._log(tick, batch_id, "归还",
                      f"整批一次归还 {pages} 页，池内剩余 {self.capacity - self._reserved} 页可用")
            self._pump_locked(tick)
            return pages

    def held_pages(self, batch_id):
        with self._lock:
            return self._held.get(batch_id, 0)

    @property
    def overtakes(self):
        with self._lock:
            return self._overtakes
