import { empty, section } from "./panel.js";
import { table } from "./table.js";
import { PoolEvents } from "../../constants/states.js";
import { el } from "../../utils/dom.js";

const EVENT_CLASS = {
  "排队": "ev-queue", "等待": "ev-wait", "领页": "ev-grant", "领页通知": "ev-grant",
  "断电中断": "ev-cut", "续跑": "ev-resume", "做完": "ev-finish", "归还": "ev-return",
  "退回": "ev-reject", "归还忽略": "ev-return",
};

function eventBadge(name) {
  return el("span", {class: `pool-event ${EVENT_CLASS[name] || "ev-queue"}`},
    PoolEvents[name] || name);
}

function timeline(events) {
  return el("div", {class: "table-scroll", tabindex: "0", "aria-label": "共享页池重演时间线", role: "region"},
    el("table", {class: "pool-timeline"},
      el("thead", {}, el("tr", {},
        ["节拍", "批次", "事件", "说明", "池内已占", "等待数"].map(h => el("th", {scope: "col"}, h)))),
      el("tbody", {}, events.map(item => el("tr", {},
        el("td", {}, String(item["时刻"])),
        el("td", {}, item["批次"]),
        el("td", {}, eventBadge(item["事件"])),
        el("td", {class: "pool-note"}, item["说明"]),
        el("td", {}, `${item["已占页"]} 页`),
        el("td", {}, String(item["等待数"])))))));
}

function batchLedger(batches) {
  return table(
    ["批次", "峰值页", "状态", "申领节拍", "领页节拍", "完成节拍", "等待原因", "断电记录"],
    batches.map(b => [b["批次"], b["峰值页"], b["状态"], b["申领时刻"], b["领页时刻"],
      b["完成时刻"], b["等待原因"], b["断电中断"]]),
    "批次台账");
}

/** 共享复演页池结果：时间线能看出等页→领页→归还，以及为什么在等。 */
export function sharedPoolReplay(result) {
  if (result?.kind !== "shared_pool") return null;
  const pool = result.pool || {};
  return [
    section("共享页池调度",
      el("p", {class: "pool-hint"},
        `总额 ${pool.capacity ?? "—"} 页，按整批峰值一次性预留；让行上限 ${pool.max_overtakes ?? "—"} 次，`,
        "被越过达上限的等待批次成为队列屏障；断电回收前暂占页仍计入总额，续跑沿用原额度，做完只还一次。"),
      el("dl", {class: "metrics"}, Object.entries(result.metrics).map(([label, value]) =>
        el("div", {}, el("dt", {}, label), el("dd", {}, String(value)))))),
    section("批次重演时间线（等页 → 领页 → 归还）",
      result.rows.length ? timeline(result.rows) : empty("此批次无重演事件。")),
    section("批次台账", result.batches?.length ? batchLedger(result.batches) : empty("无批次台账。")),
  ];
}
