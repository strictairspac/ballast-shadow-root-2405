BASE = [
    {"recipe": "BR-051", "fine_bp": 2500, "coarse_bp": 7500},
    {"recipe": "BR-052", "fine_bp": 3000, "coarse_bp": 7000},
    {"recipe": "BR-053", "fine_bp": 2800, "coarse_bp": 7200},
]
REVISION = [
    {"recipe": "BR-051", "fine_bp": 2700, "coarse_bp": 7300},
    {"recipe": "BR-052", "fine_bp": 3200, "coarse_bp": 6800},
]

FIXTURES = [
    {
        "code": "BR-K7-1124",
        "title": "K7 段配方签发重试",
        "description": "先在影子页刷盘后断电，再重试发布；比对封存根、孤儿页和释放后的页号复用。",
        "payload": {
            "initial": BASE, "keep_baseline": True,
            "events": [
                {"op": "revise", "phase": "flushed", "changes": REVISION},
                {"op": "revise", "phase": "published", "changes": REVISION},
                {"op": "revise", "phase": "published",
                 "changes": [{"recipe": "BR-053", "fine_bp": 3100, "coarse_bp": 6900}]},
                {"op": "release_baseline"},
                {"op": "revise", "phase": "published",
                 "changes": [{"recipe": "BR-053", "fine_bp": 3100, "coarse_bp": 6900}]},
            ],
        },
    },
    {
        "code": "BR-K9-1124",
        "title": "K9 段刷盘前断电",
        "description": "暂存配方未落盘时不产生孤儿页；后续正式签发与同值重试可对比。",
        "payload": {
            "initial": BASE, "keep_baseline": False,
            "events": [
                {"op": "revise", "phase": "volatile", "changes": REVISION[:1]},
                {"op": "revise", "phase": "published", "changes": REVISION[:1]},
                {"op": "revise", "phase": "published", "changes": REVISION[:1]},
            ],
        },
    },
    {
        "code": "BR-POOL-1124",
        "title": "共享复演页池·多批次公平领页",
        "description": "四张签发单共用 5 页：大单先到占满，小单让行先跑，大单被让行两次后成为屏障，超额单整单退回，含一次断电中断后用原额度续跑。",
        "payload": {
            "mode": "shared_pool",
            "batches": [
                {"id": "B-大单-01", "peak": 3, "duration": 4, "arrival": 0},
                {"id": "B-小单-02", "peak": 2, "duration": 2, "arrival": 1},
                {"id": "B-小单-03", "peak": 2, "duration": 2, "arrival": 2},
                {"id": "B-小单-04", "peak": 1, "duration": 1, "arrival": 3},
                {"id": "B-超额-05", "peak": 6, "duration": 2, "arrival": 3},
                {"id": "B-断电-06", "peak": 2, "duration": 4, "arrival": 4,
                 "interrupt": {"after": 1, "resume_after": 2}},
            ],
        },
    },
    {
        "code": "BR-POOL-1125",
        "title": "共享复演页池·断电暂占与一次归还",
        "description": "两批同峰值错峰到达；先跑批次中断期间页不回收，续跑沿用原额度，做完只还一次，后到批次随后整批领页。",
        "payload": {
            "mode": "shared_pool",
            "batches": [
                {"id": "B-A", "peak": 2, "duration": 3, "arrival": 0,
                 "interrupt": {"after": 1, "resume_after": 2}},
                {"id": "B-B", "peak": 2, "duration": 2, "arrival": 1},
            ],
        },
    },
    {
        "code": "BR-POOL-1126",
        "title": "共享复演页池·大单屏障防饥饿",
        "description": "5 页池里先跑一个占 4 页的长单，大单（4 页）排队，两个 1 页小单在剩余额度内越过大单先跑；大单被越过两次后成为队列屏障，第三个小单即使 1 页够用也必须等，长单归还后大单优先放行。",
        "payload": {
            "mode": "shared_pool",
            "batches": [
                {"id": "B-占位长单", "peak": 4, "duration": 5, "arrival": 0},
                {"id": "B-大单", "peak": 4, "duration": 2, "arrival": 1},
                {"id": "B-小单甲", "peak": 1, "duration": 1, "arrival": 2},
                {"id": "B-小单乙", "peak": 1, "duration": 1, "arrival": 3},
                {"id": "B-小单丙", "peak": 1, "duration": 1, "arrival": 4},
            ],
        },
    },
]
