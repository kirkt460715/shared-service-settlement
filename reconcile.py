"""联合上门服务的成本指标与拨付核对。

本模块只依赖标准库，处理 data/ 下登记的六个集合：

- visits               履约活动与签收单位（取消不产生单位）
- expenses             经济成本凭证（含红字冲销、空路由待分摊）
- submitted_invoices   部门提交材料（同一 receipt 跨部门提交不重复付款）
- shares               共同费用分摊协议（按生效日取版，补签不改发生期）
- funds                资助额度（同池共用上限，资格为项目+成本类型）
- allocations          已记账的承诺、支付与冲回

金额一律以分为单位的整数表示。月份按业务发生日期归属，凭证自身的提交、
签发时间只用于判断"到某个时点可不可办"，不重新定义成本发生期。
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from inspect_records import DATA, read_collection

CASH_NATURES = {"staff", "travel"}
TRANSACTIONS = ("commit", "paid", "reversed")


def _d(text):
    """把交换文件中的 ISO 日期字符串转成 date。"""
    return date.fromisoformat(text)


def _i(text):
    """交换值都是字符串，金额、数量等统一在此转整数。"""
    return int(text)


# ---------------------------------------------------------------------------
# 装载
# ---------------------------------------------------------------------------


def load_tables(directory=DATA):
    """读取全部集合并以主键建索引，返回 {集合名: {主键: 记录}}。"""
    names = ("visits", "expenses", "submitted_invoices", "shares",
             "funds", "allocations")
    tables = {}
    for name in names:
        rows, key = read_collection(name, directory)
        tables[name] = {row[key]: row for row in rows}
    return tables


# ---------------------------------------------------------------------------
# 协议选版与整数分摊
# ---------------------------------------------------------------------------


def effective_share(shares, group, on_date):
    """返回发生日适用的分摊协议版本。

    协议按 start 登记生效日，取 start <= 发生日 的最新一版；补签协议的
    start 晚于业务发生日时不适用，发生期不被重新定义。
    """
    candidates = [s for s in shares.values()
                  if s["group"] == group and _d(s["start"]) <= on_date]
    if not candidates:
        raise ValueError(f"{on_date} 的 {group} 组合没有适用的分摊协议")
    return max(candidates, key=lambda s: _d(s["start"]))


def split_shared(amount, share, programs=("bath", "escort")):
    """按协议权重把一笔整数金额拆到两个服务，尾差归 remainder_to。

    权重为 bath_num/denominator、escort_num/denominator。先向下取整分配，
    再把未分完的尾差一次性加给协议指定的优先项目，因此拆分恒等、不丢分。
    负数（冲销）同样按权重拆，余数规则保持一致。
    """
    denominator = _i(share["denominator"])
    weights = {
        "bath": _i(share["bath_num"]),
        "escort": _i(share["escort_num"]),
    }
    sign = -1 if amount < 0 else 1
    base = abs(amount)
    parts = {}
    for program in programs:
        parts[program] = (base * weights[program]) // denominator
    remainder = base - sum(parts.values())
    parts[share["remainder_to"]] += remainder
    if sign < 0:
        parts = {program: -value for program, value in parts.items()}
    assert sum(parts.values()) == amount
    return parts


# ---------------------------------------------------------------------------
# 凭证去重与路由
# ---------------------------------------------------------------------------


def dedupe_invoices(invoices):
    """按 receipt 归并部门提交材料。

    跨部门提交同一张原始凭证只证明同一笔经济成本，不构成各自垫付。
    返回 {receipt: [提交记录...]}，并给出是否重复提交的标记。
    """
    grouped = {}
    for invoice in sorted(invoices.values(), key=lambda r: r["invoice_at"]):
        grouped.setdefault(invoice["receipt"], []).append(invoice)
    return grouped


@dataclass
class CostSlice:
    """一张成本凭证路由到某一服务后的一份成本份额。"""

    evidence_id: str
    when: date
    provider: str
    program: str
    group: str
    cohort: str
    nature: str
    amount: int
    visit_ids: tuple
    is_reversal: bool
    canceled: bool
    in_kind: bool
    routed: bool
    share_id: str | None = None

    def as_dict(self):
        return {
            "evidence_id": self.evidence_id,
            "when": self.when.isoformat(),
            "provider": self.provider,
            "program": self.program,
            "group": self.group,
            "cohort": self.cohort,
            "nature": self.nature,
            "amount": self.amount,
            "visit_ids": list(self.visit_ids),
            "is_reversal": self.is_reversal,
            "canceled": self.canceled,
            "in_kind": self.in_kind,
            "routed": self.routed,
            "share_id": self.share_id,
        }


def route_expenses(tables):
    """把每张成本凭证路由成一份或多份 CostSlice。

    - 空 activity_keys：未能定位服务，整体留待分摊（routed=False）。
    - 单服务凭证：金额整体归该服务，不做权重拆分。
    - 多服务（组合）凭证：按发生日适用协议拆到涉及的各服务。
    - 冲销凭证：跟随被冲销凭证的活动路由与发生期协议，金额为负。
    取消的活动没有完成单位，但费用仍形成可解释的成本切片。
    """
    visits = tables["visits"]
    expenses = tables["expenses"]
    shares = tables["shares"]
    slices = []
    for evidence in sorted(expenses.values(), key=lambda r: (r["when"], r["evidence_id"])):
        evidence_id = evidence["evidence_id"]
        when = _d(evidence["when"])
        keys = tuple(k for k in evidence["activity_keys"].split(",") if k)
        reversal_for = evidence["reversal_for"]
        is_reversal = bool(reversal_for)
        in_kind = evidence["nature"] not in CASH_NATURES

        if not keys:
            slices.append(CostSlice(
                evidence_id, when, evidence["provider"], "", "", "",
                evidence["nature"], _i(evidence["amount"]), (),
                is_reversal, False, in_kind, False,
            ))
            continue

        target_visits = [visits[k] for k in keys]
        programs = sorted({v["program"] for v in target_visits})
        amount = _i(evidence["amount"])

        if len(programs) == 1:
            visit = target_visits[0]
            slices.append(CostSlice(
                evidence_id, when, evidence["provider"], visit["program"],
                visit["group"], visit["cohort"], evidence["nature"], amount,
                keys, is_reversal, visit["status"] == "cancel", in_kind, True,
            ))
            continue

        # 组合凭证：发生期决定协议版本；冲销跟随原凭证的发生期协议。
        basis_date = _d(expenses[reversal_for]["when"]) if is_reversal else when
        share = effective_share(shares, target_visits[0]["group"], basis_date)
        parts = split_shared(amount, share, tuple(programs))
        for program in programs:
            program_visits = [v for v in target_visits if v["program"] == program]
            cohorts = {v["cohort"] for v in program_visits}
            groups = {v["group"] for v in program_visits}
            canceled = all(v["status"] == "cancel" for v in program_visits)
            slices.append(CostSlice(
                evidence_id, when, evidence["provider"], program,
                groups.pop() if len(groups) == 1 else program_visits[0]["group"],
                cohorts.pop() if len(cohorts) == 1 else program_visits[0]["cohort"],
                evidence["nature"], parts[program],
                tuple(v["id"] for v in program_visits),
                is_reversal, canceled, in_kind, True, share["id"],
            ))
    return slices


# ---------------------------------------------------------------------------
# 单位成本
# ---------------------------------------------------------------------------


@dataclass
class UnitCostRow:
    """一个可比较口径（期间+组合+对象类别+服务+承接方）的成本与单位。"""

    month: str
    group: str
    cohort: str
    program: str
    provider: str
    cost: int
    cash_cost: int
    units: int
    visit_ids: list = field(default_factory=list)
    evidence_ids: list = field(default_factory=list)
    has_cancel: bool = False
    has_reversal: bool = False

    @property
    def unit_cost(self):
        """真实单位成本，取消与在途分摊不产生单位时为 None（不可硬算）。"""
        return None if self.units == 0 else self.cost / self.units

    @property
    def cash_unit_cost(self):
        return None if self.units == 0 else self.cash_cost / self.units

    def as_dict(self):
        data = {k: v for k, v in self.__dict__.items()}
        data["unit_cost"] = self.unit_cost
        data["cash_unit_cost"] = self.cash_unit_cost
        return data


def unit_costs(tables, slices, month=None):
    """按可累加口径汇总成本与完成单位。

    月份按业务发生期（冲销看原凭证发生期）归集。单位只统计 done 的签收
    单位，cancel 不产生单位；实物支持计入经济成本但单列，现金口径可对照。
    待分摊切片不进任何服务口径，避免污染单位成本。
    """
    visits = tables["visits"]
    expenses = tables["expenses"]
    rows = {}

    def period_of(slice_):
        if slice_.is_reversal:
            ref = expenses[slice_.evidence_id]["reversal_for"]
            return _d(expenses[ref]["when"]).strftime("%Y-%m")
        return slice_.when.strftime("%Y-%m")

    for slice_ in slices:
        if not slice_.routed:
            continue
        period = period_of(slice_)
        if month is not None and period != month:
            continue
        key = (period, slice_.group, slice_.cohort, slice_.program, slice_.provider)
        row = rows.setdefault(key, UnitCostRow(
            period, slice_.group, slice_.cohort, slice_.program, slice_.provider,
            0, 0, 0,
        ))
        row.cost += slice_.amount
        if not slice_.in_kind:
            row.cash_cost += slice_.amount
        row.has_cancel = row.has_cancel or slice_.canceled
        row.has_reversal = row.has_reversal or slice_.is_reversal
        row.evidence_ids.append(slice_.evidence_id)
        for vid in slice_.visit_ids:
            if vid not in row.visit_ids:
                row.visit_ids.append(vid)
                if visits[vid]["status"] == "done":
                    row.units += _i(visits[vid]["units"])

    # 同一凭证可能有多张切片（如冲销跟随原路由），去重后保留可追溯清单
    for row in rows.values():
        row.evidence_ids = sorted(set(row.evidence_ids))
        row.visit_ids = sorted(set(row.visit_ids))
    return [rows[k] for k in sorted(rows)]


def pending_slices(slices):
    """未能定位服务、暂留待分摊的成本。"""
    return [s for s in slices if not s.routed]


# ---------------------------------------------------------------------------
# 资助资格、共同池与已记账占用
# ---------------------------------------------------------------------------


def fund_covers(fund_row, slice_):
    """判断资助是否覆盖该成本切片：项目、成本类型、资格有效期同时满足。

    资格以资助协议登记为准；实物支持属于经济成本但不在现金资助范围内。
    """
    if slice_.in_kind:
        return False
    if slice_.program not in fund_row["program"].split(","):
        return False
    if slice_.nature not in fund_row["nature"].split(","):
        return False
    if not (_d(fund_row["active_from"]) <= slice_.when <= _d(fund_row["active_until"])):
        return False
    return True


def eligible_demand(tables, slices):
    """按资助汇总可补范围（按真实用途解释，不做科目腾挪）。"""
    funds = tables["funds"]
    demand = {fid: 0 for fid in funds}
    detail = {fid: [] for fid in funds}
    for slice_ in slices:
        if not slice_.routed or slice_.is_reversal:
            continue
        for fid, fund_row in funds.items():
            if fund_covers(fund_row, slice_):
                demand[fid] += slice_.amount
                detail[fid].append(slice_)
    return demand, detail


def pool_positions(tables, as_of=None):
    """计算各资助池的额度占用与剩余可承诺空间。

    同一 claim 的承诺与实付是同一笔额度的两个阶段，按 max(commit, 实付净额)
    占用，避免一笔业务双重占额；reversed 只有实际冲回后才减少实付占用。
    联合凭证下各资助共用同一 pool_cap，不能按每张票各自绕过共同上限。
    as_of 用于历史复核：只统计该日期（含）以前已记账的事项。
    """
    allocations = tables["allocations"]
    funds = tables["funds"]
    grouped = {}
    for alloc in allocations.values():
        if as_of is not None and _d(alloc["at"]) > as_of:
            continue
        grouped.setdefault((alloc["fund"], alloc["claim"]), []).append(alloc)

    occupied_by_fund = {fid: 0 for fid in funds}
    commit_by_fund = {fid: 0 for fid in funds}
    paid_net_by_fund = {fid: 0 for fid in funds}
    for (fid, _claim), items in grouped.items():
        commit = sum(_i(a["amount"]) for a in items if a["transaction"] == "commit")
        paid = sum(_i(a["amount"]) for a in items if a["transaction"] == "paid")
        reversed_ = sum(_i(a["amount"]) for a in items if a["transaction"] == "reversed")
        paid_net = max(0, paid - reversed_)
        commit_by_fund[fid] += commit
        paid_net_by_fund[fid] += paid_net
        occupied_by_fund[fid] += max(commit, paid_net)

    pools = {}
    for fid, fund_row in funds.items():
        pool = fund_row["pool"]
        entry = pools.setdefault(pool, {
            "pool": pool, "cap": _i(fund_row["pool_cap"]),
            "occupied": 0, "funds": {},
        })
        entry["funds"][fid] = {
            "occupied": occupied_by_fund[fid],
            "commit": commit_by_fund[fid],
            "paid_net": paid_net_by_fund[fid],
        }
    for entry in pools.values():
        entry["occupied"] = sum(v["occupied"] for v in entry["funds"].values())
        entry["available"] = entry["cap"] - entry["occupied"]
    return pools


# ---------------------------------------------------------------------------
# 签发判断与冲销处理
# ---------------------------------------------------------------------------


def invoice_decisions(tables):
    """归口视图：部门提交材料分别对应什么处理结论。

    同一 receipt 的首次提交为原始受理，其后的跨部门提交标记 duplicate，
    不形成第二笔付款；未被任何部门提交的凭证不能据以签发。
    """
    grouped = dedupe_invoices(tables["submitted_invoices"])
    decisions = []
    for receipt, items in grouped.items():
        first = items[0]
        decisions.append({
            "invoice_id": first["id"], "dept": first["dept"],
            "receipt": receipt, "state": first["state"],
            "duplicate_of": None,
            "note": "original" if len(items) == 1 else "first_submission",
        })
        for later in items[1:]:
            decisions.append({
                "invoice_id": later["id"], "dept": later["dept"],
                "receipt": receipt, "state": later["state"],
                "duplicate_of": first["id"],
                "note": "same_receipt_duplicate_not_a_separate_payment",
            })
    return sorted(decisions, key=lambda d: d["invoice_id"])


def claimable_slices(tables, slices, as_of=None):
    """已被合法受理凭证覆盖、可进入拨付判断的成本切片（含每片的资助选项）。

    as_of 历史复核时，截止日之后才提交的材料视为当时尚不可用。
    """
    grouped = dedupe_invoices(tables["submitted_invoices"])
    accepted = set()
    for receipt, items in grouped.items():
        for invoice in items:
            if invoice["state"] != "accepted":
                continue
            if as_of is not None and _d(invoice["invoice_at"]) > as_of:
                continue
            accepted.add(receipt)
            break
    result = []
    for slice_ in slices:
        if not slice_.routed or slice_.is_reversal or slice_.in_kind:
            continue
        options = [fid for fid, row in tables["funds"].items()
                   if fund_covers(row, slice_)]
        result.append((slice_, options, slice_.evidence_id in accepted))
    return result


def issuance_plan(tables, slices, as_of=None):
    """在共同池上限内给出可签发/受限/材料不足的判断，不做跨科目腾挪。

    已记账占用先锁定额度；剩余空间按凭证发生顺序分配，资助选择以资格
    精确（限定项目更少）者优先。超限部分保留在真实用途科目下，标记
    cap_blocked，而不是挪到别的科目假装已获资助。
    """
    pools = pool_positions(tables, as_of)
    remaining = {pool: entry["available"] for pool, entry in pools.items()}
    plan = []
    for slice_, options, documented in claimable_slices(tables, slices, as_of):
        if not documented:
            status, fund_id, amount_out = "missing_document", None, 0
        elif not options:
            status, fund_id, amount_out = "no_eligible_fund", None, 0
        else:
            ranked = sorted(options, key=lambda f: (
                len(tables["funds"][f]["program"].split(",")), f))
            fund_id = next(
                (f for f in ranked
                 if remaining[tables["funds"][f]["pool"]] >= slice_.amount),
                None)
            if fund_id is None:
                status = "cap_blocked"
                amount_out = 0
            else:
                status = "issuable"
                amount_out = slice_.amount
                remaining[tables["funds"][fund_id]["pool"]] -= amount_out
        plan.append({
            "evidence_id": slice_.evidence_id,
            "program": slice_.program,
            "nature": slice_.nature,
            "amount": slice_.amount,
            "fund": fund_id,
            "issuable_amount": amount_out,
            "status": status,
            "options": options,
        })
    return plan, pools


def reversal_report(tables):
    """冲销/红字处理：应撤回的来源、可释放的实付额度与正确去向。

    只有实际登记 reversed 以后才释放实付额度；本数据没有改归项目的情形，
    因此呈现可追溯的撤回链路与历史签发单复核所需的当时事实。
    """
    expenses = tables["expenses"]
    allocations = tables["allocations"]
    reports = []
    for evidence in expenses.values():
        if not evidence["reversal_for"]:
            continue
        original_id = evidence["reversal_for"]
        reversal_amount = _i(evidence["amount"])  # 负数
        linked = [a for a in allocations.values() if a["cost"] == original_id]
        paid = sum(_i(a["amount"]) for a in linked if a["transaction"] == "paid")
        reversed_ = sum(_i(a["amount"]) for a in linked
                        if a["transaction"] == "reversed")
        reports.append({
            "reversal": evidence["evidence_id"],
            "original": original_id,
            "reversal_amount": reversal_amount,
            "routed_follow_original": True,
            "allocations": sorted({(a["fund"], a["claim"]) for a in linked}),
            "paid_before": paid,
            "actually_reversed": reversed_,
            "paid_released_now": reversed_,
            "paid_still_outstanding": max(0, paid - reversed_),
            "retarget": None,
        })
    return reports


# ---------------------------------------------------------------------------
# 月度总账
# ---------------------------------------------------------------------------


def _period_of_slice(slice_, expenses):
    """切片归属期间：冲销回到被冲销凭证的发生期，其余看自身发生日。"""
    if slice_.is_reversal:
        ref = expenses[slice_.evidence_id]["reversal_for"]
        return expenses[ref]["when"][:7]
    return slice_.when.strftime("%Y-%m")


def monthly_close(tables, month=None, as_of=None):
    """重做某月（或全期）分析与对账，一处入口贯通凭证到资金。

    month 收口成本与单位口径（冲销归原凭证发生期）；资助池、拨付判断与
    冲销链路是时点状态，始终按 as_of 全期呈现，避免把额度占用切碎。
    """
    all_slices = route_expenses(tables)
    if month is None:
        slices = all_slices
    else:
        slices = [s for s in all_slices
                  if _period_of_slice(s, tables["expenses"]) == month]
    rows = unit_costs(tables, slices, month)
    plan, pools = issuance_plan(tables, all_slices, as_of)
    demand, _ = eligible_demand(tables, slices)
    routed_total = sum(s.amount for s in slices if s.routed)
    economic_total = sum(s.amount for s in slices)
    return {
        "month": month or "ALL",
        "as_of": as_of.isoformat() if isinstance(as_of, date) else as_of,
        "totals": {
            "economic_cost": economic_total,
            "routed_cost": routed_total,
            "pending_cost": economic_total - routed_total,
            "done_units": sum(_i(v["units"]) for v in tables["visits"].values()
                              if v["status"] == "done"
                              and (month is None or v["when"].startswith(month))),
        },
        "unit_costs": [r.as_dict() for r in rows],
        "pending": [s.as_dict() for s in pending_slices(slices)],
        "slices": [s.as_dict() for s in slices],
        "fund_demand": demand,
        "pools": pools,
        "issuance_plan": plan,
        "invoice_decisions": invoice_decisions(tables),
        "reversals": reversal_report(tables),
    }


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def _format_yuan(cents):
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) / 100:.2f}元"


def print_report(report):
    """输出便于主管阅读的中文月结报告。"""
    t = report["totals"]
    print(f"== 月结分析 {report['month']} ==")
    print(f"经济成本合计 {_format_yuan(t['economic_cost'])}，"
          f"已路由 {_format_yuan(t['routed_cost'])}，"
          f"待分摊 {_format_yuan(t['pending_cost'])}，"
          f"完成单位 {t['done_units']}")
    print("\n-- 单位成本（按 期间/组合/类别/服务/承接方）--")
    for row in report["unit_costs"]:
        unit = "无完成单位" if row["unit_cost"] is None else _format_yuan(round(row["unit_cost"]))
        flags = "".join([
            "含取消" if row["has_cancel"] else "",
            "、含冲销" if row["has_reversal"] else "",
        ])
        print(f"{row['month']} {row['group']}/{row['cohort']}/{row['program']}"
              f"/{row['provider']}: 成本{_format_yuan(row['cost'])} "
              f"单位{row['units']} 单位成本{unit} {flags} "
              f"凭证{','.join(row['evidence_ids'])}")
    print("\n-- 待分摊（未定位服务）--")
    for s in report["pending"]:
        print(f"{s['evidence_id']} {s['when']} {s['nature']} "
              f"{_format_yuan(s['amount'])}")
    print("\n-- 资助池占用 --")
    for pool in report["pools"].values():
        print(f"{pool['pool']}: 上限{_format_yuan(pool['cap'])} "
              f"已占用{_format_yuan(pool['occupied'])} "
              f"可承诺{_format_yuan(pool['available'])}")
        for fid, v in pool["funds"].items():
            print(f"  {fid}: 占用{_format_yuan(v['occupied'])} "
                  f"(承诺{_format_yuan(v['commit'])}/净实付{_format_yuan(v['paid_net'])})")
    print("\n-- 拨付判断 --")
    for item in report["issuance_plan"]:
        print(f"{item['evidence_id']} {item['program']}/{item['nature']} "
              f"{_format_yuan(item['amount'])} -> {item['status']} "
              f"资助{item['fund'] or '-'} 可签{_format_yuan(item['issuable_amount'])}")
    print("\n-- 凭证归口 --")
    for d in report["invoice_decisions"]:
        print(f"{d['invoice_id']} {d['dept']} receipt={d['receipt']} {d['note']}")
    print("\n-- 冲销 --")
    for r in report["reversals"]:
        print(f"{r['reversal']} 冲销 {r['original']} "
              f"{_format_yuan(r['reversal_amount'])}，已实际冲回"
              f"{_format_yuan(r['actually_reversed'])}，"
              f"仍在外净实付{_format_yuan(r['paid_still_outstanding'])}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="联合上门服务月结对账")
    parser.add_argument("--month", help="结账月份 YYYY-MM，缺省为全期")
    parser.add_argument("--as-of", help="历史复核截止日 YYYY-MM-DD")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非文本报告")
    args = parser.parse_args(argv)
    as_of = _d(args.as_of) if args.as_of else None
    report = monthly_close(load_tables(), args.month, as_of)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
