"""共享页池的确定性重演驱动器。

按虚拟节拍重放多张配方签发单（批次）的一生：
到达申领 → 排队/等待（带原因）→ 整批峰值领页 → 运行中可断电中断
（暂占页不回收）→ 用原额度续跑 → 做完只归还一次。

驱动器只用 :class:`SharedPagePool` 的非阻塞接口，调度逻辑与真实并发线程
完全一致，但同样的输入必然产生同样的事件流，便于发布重演与验收留档。
事件流由页池统一维护，保证同拍内的事件按真实发生顺序排列。
"""

import re

from .pool import SharedPagePool

BATCH_STATES = {
    "waiting": "等待领页",
    "running": "运行中",
    "interrupted": "断电中断",
    "resumed": "续跑中",
    "done": "已完成",
    "rejected": "额度退回",
}

_MAX_TICKS = 2000
_ID_RE = re.compile(r"[一-龥A-Za-z0-9_-]{1,24}")


def _batch_spec(row):
    if not isinstance(row, dict):
        raise ValueError("batches 各项必须是对象")
    batch_id = row.get("id")
    if not isinstance(batch_id, str) or not _ID_RE.fullmatch(batch_id):
        raise ValueError("批次 id 仅允许 1 至 24 位中文、字母、数字、下划线或连字符")
    peak = row.get("peak")
    duration = row.get("duration")
    if type(peak) is not int or peak < 1:
        raise ValueError("批次 peak（整批峰值页数）须为正整数")
    if type(duration) is not int or duration < 1:
        raise ValueError("批次 duration（运行节拍数）须为正整数")
    arrival = row.get("arrival", 0)
    if type(arrival) is not int or arrival < 0:
        raise ValueError("批次 arrival（到达节拍）须为非负整数")
    spec = {"id": batch_id, "peak": peak, "duration": duration, "arrival": arrival,
            "interrupt": None}
    interruption = row.get("interrupt")
    if interruption is not None:
        if not isinstance(interruption, dict):
            raise ValueError("interrupt 须为对象")
        after = interruption.get("after")
        resume_after = interruption.get("resume_after")
        if type(after) is not int or not 1 <= after < duration:
            raise ValueError("interrupt.after 须为正整数且小于 duration")
        if type(resume_after) is not int or resume_after < 1:
            raise ValueError("interrupt.resume_after 须为正整数")
        spec["interrupt"] = {"after": after, "resume_after": resume_after}
    return spec


def normalize_pool(payload):
    if not isinstance(payload, dict):
        raise ValueError("输入需为对象")
    if payload.get("mode") != "shared_pool":
        raise ValueError("共享页池重演需 mode=shared_pool")
    rows = payload.get("batches")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
        raise ValueError("batches 须有 1 至 32 个批次")
    specs = [_batch_spec(row) for row in rows]
    if len({spec["id"] for spec in specs}) != len(specs):
        raise ValueError("batches 含重复批次 id")
    return specs


def run_shared(payload, capacity):
    if type(capacity) is not int or capacity < 1:
        raise ValueError("共享页池总额须为正整数")
    specs = normalize_pool(payload)
    specs_by_id = {spec["id"]: spec for spec in specs}
    pool = SharedPagePool(capacity)
    arrivals = {}
    ledgers = {}
    for spec in specs:
        arrivals.setdefault(spec["arrival"], []).append(spec)
        ledgers[spec["id"]] = {
            "批次": spec["id"], "峰值页": spec["peak"], "状态": BATCH_STATES["waiting"],
            "申领时刻": spec["arrival"], "领页时刻": "—", "完成时刻": "—",
            "等待原因": "—", "断电中断": "—", "占用页": 0,
        }

    def wait_note():
        """从池事件取每个等待批次最新一条等待原因。"""
        latest = {}
        for item in pool.events():
            if item["事件"] == "等待":
                latest[item["批次"]] = item["说明"]
        return latest

    def sync_grants(tick):
        """把 pump 后新领到页的等待批次转成运行台账。"""
        notes = wait_note()
        for batch_id, ledger in ledgers.items():
            if ledger["状态"] == BATCH_STATES["waiting"]:
                ledger["等待原因"] = notes.get(batch_id, ledger["等待原因"])
            if ledger["状态"] == BATCH_STATES["waiting"] and pool.held_pages(batch_id):
                spec = specs_by_id[batch_id]
                ledger["状态"] = BATCH_STATES["running"]
                ledger["领页时刻"] = tick
                ledger["占用页"] = spec["peak"]
                active[batch_id] = {**spec, "running_ticks": 0, "cut": False, "pause_until": None}
                pool.log(tick, batch_id, "领页通知",
                         f"排到页额，按整批峰值 {spec['peak']} 页开跑")

    active = {}
    returned_pages = 0
    tick = 0
    pending = {b["id"] for b in specs}
    while pending and tick <= _MAX_TICKS:
        # 1) 本拍到达的批次按登记顺序申领。
        for spec in arrivals.pop(tick, []):
            batch_id = spec["id"]
            granted, reason = pool.request(batch_id, spec["peak"], tick)
            if spec["peak"] > capacity:
                ledgers[batch_id]["状态"] = BATCH_STATES["rejected"]
                ledgers[batch_id]["等待原因"] = reason
                pending.discard(batch_id)
                continue
            if not granted:
                ledgers[batch_id]["状态"] = BATCH_STATES["waiting"]
                ledgers[batch_id]["等待原因"] = reason
        sync_grants(tick)

        # 2) 到期的中断批次用原额度续跑（暂占页始终未回收）。
        for batch_id, job in list(active.items()):
            if job["cut"] and job["pause_until"] == tick:
                held = pool.held_pages(batch_id)
                assert held == job["peak"], "续跑必须沿用原额度，不能重复领页"
                job["cut"] = False
                ledgers[batch_id]["状态"] = BATCH_STATES["resumed"]
                pool.log(tick, batch_id, "续跑",
                         f"复启后沿用原预留 {held} 页，不重新领页、不重复计数")

        # 3) 推进运行：本拍末断电中断，或做完后整笔归还一次。
        for batch_id, job in list(active.items()):
            ledger = ledgers[batch_id]
            if job["cut"]:
                continue
            job["running_ticks"] += 1
            interruption = job["interrupt"]
            if (interruption and job["running_ticks"] == interruption["after"]
                    and job["running_ticks"] < job["duration"]):
                job["cut"] = True
                job["pause_until"] = tick + 1 + interruption["resume_after"]
                held = pool.held_pages(batch_id)
                ledger["状态"] = BATCH_STATES["interrupted"]
                ledger["断电中断"] = f"第 {tick} 拍断电，暂占 {held} 页不回收"
                pool.log(tick, batch_id, "断电中断",
                         f"影子页刷盘后断电，暂占的 {held} 页在回收前仍计入总额，"
                         f"暂停 {interruption['resume_after']} 拍后用原额度续跑")
                continue
            if job["running_ticks"] >= job["duration"]:
                ledger["状态"] = BATCH_STATES["done"]
                ledger["完成时刻"] = tick
                ledger["占用页"] = 0
                pool.log(tick, batch_id, "做完", "配方跑完，整批只归还一次")
                returned_pages += pool.release(batch_id, tick)
                active.pop(batch_id)
                pending.discard(batch_id)

        # 4) 归还后重新公平调度，让够用的小单和被放行的等待者开跑。
        pool.pump(tick)
        sync_grants(tick)
        tick += 1

    if tick > _MAX_TICKS:
        raise ValueError("重演超过节拍上限，可能存在无法终结的等待")

    events = pool.events()
    snapshot = pool.snapshot()
    assert snapshot.reserved <= capacity
    yield_count = pool.overtakes
    done = [b for b in ledgers.values() if b["状态"] == BATCH_STATES["done"]]
    rejected = [b for b in ledgers.values() if b["状态"] == BATCH_STATES["rejected"]]
    waited = [b for b in ledgers.values() if b["等待原因"] != "—"
              and b["状态"] != BATCH_STATES["rejected"]]
    interrupted = [b for b in ledgers.values() if b["断电中断"] != "—"]
    peak_used = max((item["已占页"] for item in events), default=0)
    summary = (
        f"共享页池 {capacity} 页：完成 {len(done)} 批、退回 {len(rejected)} 批、"
        f"等待 {len(waited)} 批、断电续跑 {len(interrupted)} 批；"
        f"峰值并发占用 {peak_used} 页，全程未超限，让行 {yield_count} 次、归还 {returned_pages} 页次。"
    )
    return {
        "kind": "shared_pool",
        "summary": summary,
        "metrics": {
            "共享页总额": capacity,
            "峰值并发占用": peak_used,
            "完成批次数": len(done),
            "退回批次数": len(rejected),
            "等待批次数": len(waited),
            "断电续跑批次数": len(interrupted),
            "让行次数": yield_count,
            "归还总页次": returned_pages,
        },
        "rows": events,
        "batches": list(ledgers.values()),
        "pool": {"capacity": capacity, "max_overtakes": pool.max_overtakes,
                 "final_reserved": snapshot.reserved, "final_waiting": len(snapshot.waiting)},
    }
