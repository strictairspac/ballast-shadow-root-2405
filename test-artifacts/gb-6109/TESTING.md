# 验收记录：共享复演页池

日期：2026-11-26
范围：多张配方签发单共用一份复演页额的领页、等待、归还与发布重演。

## 需求对照

| 需求口径 | 实现位置 | 验证方式 |
|---|---|---|
| 几张签发单共用一份页额，各跑各的配方 | `backend/src/domain/pool.py` `SharedPagePool` | 单元测试 + 确定性重演 |
| 领页按整批峰值算 | `request/acquire` 一次性预留 `peak` 页 | `test_grant_by_batch_peak_and_invariant` |
| 断电回收前暂占的页也算额度 | 中断期间不释放预留，`reserved` 不变 | `test_interrupted_batch_keeps_pages_until_single_return`、BR-POOL-1125 |
| 并发领页总占额不超限 | 全部状态变更在同一把锁内，断言 `reserved <= capacity` | `test_concurrent_reservation_never_exceeds_capacity`、`test_randomized_concurrent_invariant_holds` |
| 单批超过总额退回 | `peak > capacity` 直接退回且不占页 | `test_peak_over_capacity_is_rejected_without_holding`、BR-POOL-1124 的 B-超额-05 |
| 够用的小单可以先跑 | 队头让行未达上限时可被越过 | BR-POOL-1126（小单甲、小单乙先跑） |
| 大单不能一直被插队 | 让行达 2 次后队头成为队列屏障 | `test_small_batch_overtakes_then_barrier_blocks_starvation`、BR-POOL-1126 |
| 中断后用原额度续跑 | 续跑不重新领页，沿用原预留 | `test_interrupted_batch_resumes_with_same_quota_and_returns_once` |
| 做完只还一次 | `release` 对同一批次仅首次生效 | `test_release_happens_once`、每批归还事件恰为 1 条 |
| 重演页能看到等页→领页→归还及等待原因 | `pool_scenario` 时间线 + 前端 `pool_replay.js` | 渲染冒烟测试、事件流人工核对 |

## 自动化测试

纯标准库 `unittest`，无需 Docker 或第三方依赖：

```sh
cd backend
python3 -I -m unittest discover -s tests -t . -v
```

连续多轮结果：`Ran 15 tests ... OK`（含 6 线程随机申领/归还压力测试，多轮稳定）。

## 确定性重演指标（页池参数均为 5）

| 资料 | 完成 | 退回 | 等待 | 断电续跑 | 让行 | 峰值占用 | 归还页次 | 末态占用 |
|---|---|---|---|---|---|---|---|---|
| BR-POOL-1124 多批次公平领页 | 5 | 1 | 2 | 1 | 0 | 5 | 10 | 0 |
| BR-POOL-1125 断电暂占与一次归还 | 2 | 0 | 0 | 1 | 0 | 4 | 4 | 0 |
| BR-POOL-1126 大单屏障防饥饿 | 5 | 0 | 2 | 0 | 2 | 5 | 11 | 0 |

关键时序（BR-POOL-1126）：

1. 占位长单占 4 页，大单（4 页）到达后入队等待。
2. 小单甲、小单乙各 1 页，在剩余额度内越过大单先跑，大单让行计数依次变为 1、2。
3. 大单成为队列屏障；小单丙即使 1 页够用也在其后等待。
4. 占位长单归还后大单立即领页，随后小单丙放行；全批峰值占用始终不超过 5 页。

## 前端验证

- 所有 ES Module 通过 `node --check` 语法检查。
- `pool_replay.js` 在最小 DOM 垫片下渲染冒烟通过：输出「共享页池调度 / 批次重演时间线 / 批次台账」三个区块，包含等待原因、领页与归还事件；非共享池结果返回 `null`，旧影子分页页面不受影响。
- 事件徽章按类型着色（等待、领页、断电中断、续跑、归还、退回），时间线列含节拍、批次、事件、说明、池内已占、等待数。

## 兼容性

- 原有影子分页场景（BR-K7-1124、BR-K9-1124）结果结构不变，回归测试 `test_legacy_shadow_pager_scenario_still_works` 通过。
- 数据库表结构、API 路由、账号与提交流程均未改动；共享池资料通过现有 `cases`/`runs` 接口提交，仅以 payload 中 `mode=shared_pool` 区分。
