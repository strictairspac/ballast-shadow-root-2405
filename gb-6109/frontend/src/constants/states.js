export const States = Object.freeze({
  queued: "等待执行", done: "已完成", failed: "执行失败",
});

export const BatchStates = Object.freeze({
  "等待领页": "等待领页",
  "运行中": "运行中",
  "断电中断": "断电中断",
  "续跑中": "续跑中",
  "已完成": "已完成",
  "额度退回": "额度退回",
});

export const PoolEvents = Object.freeze({
  "排队": "排队", "等待": "等待", "领页": "领页", "领页通知": "领页",
  "断电中断": "断电中断", "续跑": "续跑", "做完": "做完", "归还": "归还",
  "退回": "退回", "归还忽略": "归还忽略",
});

export const Actions = Object.freeze({
  create_case: "登记资料", enqueue: "提交执行", done: "执行完成", failed: "执行失败", review: "复核签署",
});
