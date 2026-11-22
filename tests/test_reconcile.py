"""联合上门服务成本与拨付核对的业务规则测试。"""
import unittest
from datetime import date

from inspect_records import read_collection, DATA
import reconcile as rc


class ReconcileTestBase(unittest.TestCase):
    def setUp(self):
        self.tables = rc.load_tables()
        self.slices = rc.route_expenses(self.tables)

    def slice(self, evidence, program):
        return [s for s in self.slices
                if s.evidence_id == evidence and s.program == program]

    def one(self, evidence, program):
        matches = self.slice(evidence, program)
        self.assertEqual(len(matches), 1)
        return matches[0]


class AllocationTests(ReconcileTestBase):
    def test_split_remainder_to_priority_program(self):
        # S1: 权重 2/3 与 1/3，10000 分不出整数，尾差归 bath
        share = self.tables["shares"]["S1"]
        self.assertEqual(rc.split_shared(10000, share),
                         {"bath": 6667, "escort": 3333})
        # S2: 1/1 对分，尾差归 escort
        share2 = self.tables["shares"]["S2"]
        self.assertEqual(rc.split_shared(12001, share2),
                         {"bath": 6000, "escort": 6001})

    def test_split_negative_reversal_keeps_weight_and_remainder(self):
        share = self.tables["shares"]["S1"]
        parts = rc.split_shared(-2500, share)
        self.assertEqual(parts, {"bath": -1667, "escort": -833})
        self.assertEqual(sum(parts.values()), -2500)

    def test_share_version_by_occurrence_date_not_signing(self):
        shares = self.tables["shares"]
        # 5 月业务适用 S1；6 月 15 日之前仍适用 S1，之后才是 S2
        self.assertEqual(
            rc.effective_share(shares, "combo", date(2026, 5, 12))["id"], "S1")
        self.assertEqual(
            rc.effective_share(shares, "combo", date(2026, 6, 4))["id"], "S1")
        self.assertEqual(
            rc.effective_share(shares, "combo", date(2026, 6, 16))["id"], "S2")

    def test_e01_routed_under_s1_with_remainder_to_bath(self):
        self.assertEqual(self.one("E01", "bath").amount, 6667)
        self.assertEqual(self.one("E01", "escort").amount, 3333)
        self.assertEqual(self.one("E01", "bath").share_id, "S1")

    def test_e05_routed_under_mid_month_s2(self):
        # 6 月 16 日业务必须用 S2（1:1），不能沿用 5 月的 2:1
        self.assertEqual(self.one("E05", "bath").amount, 6000)
        self.assertEqual(self.one("E05", "escort").amount, 6000)
        self.assertEqual(self.one("E05", "bath").share_id, "S2")

    def test_single_service_evidence_not_weight_split(self):
        e06 = self.one("E06", "bath")
        self.assertEqual(e06.amount, 8000)
        self.assertIsNone(e06.share_id)

    def test_reversal_follows_original_routing_and_period_agreement(self):
        e08_bath = self.one("E08", "bath")
        e08_escort = self.one("E08", "escort")
        self.assertTrue(e08_bath.is_reversal)
        # 冲销按原凭证 E01 的发生期协议 S1 拆，负数尾差仍归 bath
        self.assertEqual(e08_bath.amount, -1667)
        self.assertEqual(e08_escort.amount, -833)
        self.assertEqual(e08_bath.share_id, "S1")

    def test_routed_total_conserved_and_pending_separate(self):
        # 同一成本不会因两张凭证计算两次：路由金额恒等于凭证金额
        by_evidence = {}
        for s in self.slices:
            if s.routed:
                by_evidence[s.evidence_id] = by_evidence.get(s.evidence_id, 0) + s.amount
        for evidence_id, total in by_evidence.items():
            self.assertEqual(total, int(self.tables["expenses"][evidence_id]["amount"]))
        pending = rc.pending_slices(self.slices)
        self.assertEqual([s.evidence_id for s in pending], ["E07"])
        self.assertEqual(pending[0].amount, 6000)

    def test_canceled_visit_keeps_cost_but_no_units(self):
        e03 = self.one("E03", "bath")
        self.assertTrue(e03.canceled)
        self.assertEqual(e03.amount, 2000)  # 空跑费用可解释
        row = next(r for r in rc.unit_costs(self.tables, self.slices)
                   if "V03" in r.visit_ids)
        self.assertEqual(row.units, 0)
        self.assertIsNone(row.unit_cost)  # 没有完成单位，不硬算单位成本

    def test_in_kind_is_economic_cost_but_not_cash(self):
        e04 = self.one("E04", "bath")
        self.assertTrue(e04.in_kind)
        row = next(r for r in rc.unit_costs(self.tables, self.slices)
                   if "V05" in r.visit_ids)
        self.assertGreater(row.cost, row.cash_cost)
        # 实物支持不进入任何现金资助拨付判断
        claimable_ids = {s.evidence_id
                         for s, _, _ in rc.claimable_slices(self.tables, self.slices)}
        self.assertNotIn("E04", claimable_ids)


class UnitCostTests(ReconcileTestBase):
    def test_may_and_june_rollups_and_drilldown(self):
        rows = rc.unit_costs(self.tables, self.slices)
        index = {(r.month, r.group, r.program, r.provider): r for r in rows}
        # 5 月 bath/A 含 E01 与 E08 冲销，净额可点回两张凭证
        may_bath = index[("2026-05", "combo", "bath", "A")]
        self.assertEqual(may_bath.cost, 5000)
        self.assertEqual(may_bath.units, 1)
        self.assertEqual(may_bath.unit_cost, 5000.0)
        self.assertIn("E01", may_bath.evidence_ids)
        self.assertIn("E08", may_bath.evidence_ids)
        # 6 月 escort/B 只来自 E05 的 S2 对分
        june_escort = index[("2026-06", "combo", "escort", "B")]
        self.assertEqual(june_escort.cost, 6000)
        self.assertEqual(june_escort.units, 1)

    def test_month_filter_keeps_reversal_in_original_period(self):
        june = rc.unit_costs(self.tables, self.slices, "2026-06")
        # E08 虽签于 6 月底，冲销归属原成本发生期 5 月
        self.assertFalse(any("E08" in r.evidence_ids for r in june))
        may = rc.unit_costs(self.tables, self.slices, "2026-05")
        self.assertTrue(any("E08" in r.evidence_ids for r in may))

    def test_cross_program_total_reconciles_to_evidence(self):
        report = rc.monthly_close(self.tables)
        # 跨项目汇总回到原凭证：经济成本 40100，路由 34100，待分摊 6000
        self.assertEqual(report["totals"]["economic_cost"], 40100)
        self.assertEqual(report["totals"]["routed_cost"], 34100)
        self.assertEqual(report["totals"]["pending_cost"], 6000)
        self.assertEqual(report["totals"]["done_units"], 7)

    def test_monthly_close_scopes_cost_but_keeps_fund_view_full(self):
        may = rc.monthly_close(self.tables, "2026-05")
        # 5 月：E01/E02/E03 共 15000，冲销 E08 归 5 月减 2500，净额 12500
        self.assertEqual(may["totals"]["economic_cost"], 12500)
        self.assertEqual(may["totals"]["routed_cost"], 12500)
        self.assertEqual(may["totals"]["pending_cost"], 0)
        self.assertEqual(may["totals"]["done_units"], 2)
        # 6 月才出现的待分摊 E07 不进 5 月口径
        self.assertEqual(may["pending"], [])
        june = rc.monthly_close(self.tables, "2026-06")
        # 6 月：E04-E07 共 27600，E08 已归 5 月
        self.assertEqual(june["totals"]["economic_cost"], 27600)
        self.assertEqual(june["totals"]["pending_cost"], 6000)
        # 资金占用是时点状态，不随月份切碎
        self.assertEqual(june["pools"]["joint-grant"]["occupied"], 9800)


class InvoiceAndFundTests(ReconcileTestBase):
    def test_duplicate_receipt_is_not_separate_payment(self):
        decisions = {d["invoice_id"]: d for d in rc.invoice_decisions(self.tables)}
        self.assertIsNone(decisions["I01"]["duplicate_of"])
        self.assertEqual(decisions["I02"]["duplicate_of"], "I01")
        # E01 只有一张凭证，切片金额不随两次提交翻倍
        self.assertEqual(
            sum(s.amount for s in self.slices if s.evidence_id == "E01"), 10000)

    def test_eligibility_uses_registered_scope_and_dates(self):
        f_n = self.tables["funds"]["F-N"]
        f_w = self.tables["funds"]["F-W"]
        bath_staff = self.one("E01", "bath")
        escort_staff = self.one("E01", "escort")
        self.assertTrue(rc.fund_covers(f_n, bath_staff))
        self.assertTrue(rc.fund_covers(f_n, escort_staff))
        self.assertFalse(rc.fund_covers(f_w, bath_staff))  # F-W 只覆盖 escort
        self.assertTrue(rc.fund_covers(f_w, escort_staff))

    def test_joint_pool_shared_cap_with_commit_paid_reversal(self):
        pools = rc.pool_positions(self.tables)
        joint = pools["joint-grant"]
        # 同一 claim C01：commit 6600 与净实付 4000 不重复占额，取 max；
        # 再加 C02 承诺 3200，合计占用 9800，共同上限 10000
        self.assertEqual(joint["cap"], 10000)
        self.assertEqual(joint["occupied"], 9800)
        self.assertEqual(joint["available"], 200)
        self.assertEqual(joint["funds"]["F-N"]["paid_net"], 4000)
        self.assertEqual(joint["funds"]["F-N"]["commit"], 9800)

    def test_reversal_releases_only_actually_returned_paid(self):
        report = rc.reversal_report(self.tables)[0]
        self.assertEqual(report["original"], "E01")
        self.assertEqual(report["paid_before"], 5000)
        self.assertEqual(report["actually_reversed"], 1000)
        self.assertEqual(report["paid_released_now"], 1000)
        self.assertEqual(report["paid_still_outstanding"], 4000)

    def test_issuance_plan_respects_cap_and_real_usage(self):
        plan, pools = rc.issuance_plan(self.tables, self.slices)
        status = {(p["evidence_id"], p["program"]): p for p in plan}
        # E02 bath/travel 先走更精确的地方额度 F-A，可签 2000
        e02_bath = status[("E02", "bath")]
        self.assertEqual(e02_bath["status"], "issuable")
        self.assertEqual(e02_bath["fund"], "F-A")
        self.assertEqual(e02_bath["issuable_amount"], 2000)
        # 联合池只剩 200，E02 escort/travel 需 1000，超限留在原科目
        self.assertEqual(status[("E02", "escort")]["status"], "cap_blocked")
        # E03 没有任何部门提交，材料不足，不能签发
        self.assertEqual(status[("E03", "bath")]["status"], "missing_document")
        # E05、E06 均受共同上限约束，不得拆票绕过
        self.assertTrue(all(
            status[(e, p)]["status"] == "cap_blocked"
            for e, p in (("E05", "bath"), ("E05", "escort"), ("E06", "bath"))))

    def test_demand_keeps_over_cap_cost_in_true_nature(self):
        demand, _ = rc.eligible_demand(self.tables, self.slices)
        # 可补范围按真实用途汇总毛需求，冲销单独反映；超限也不挪科目：
        # F-N = E01 毛额 10000 + E02 3000 + E05 12000 + E06 8000 = 35000
        self.assertEqual(demand["F-N"], 35000)
        # F-W 只覆盖 escort/staff：E01 的 3333 + E05 的 6000
        self.assertEqual(demand["F-W"], 9333)
        # F-A 覆盖 bath/travel：E02 的 2000 + E03 空跑 2000
        self.assertEqual(demand["F-A"], 4000)

    def test_as_of_history_review(self):
        # 截止 6 月 10 日：E05/E06/E08 尚未提交，且 7 月的冲回尚未登记
        as_of = date(2026, 6, 10)
        pools = rc.pool_positions(self.tables, as_of)
        self.assertEqual(pools["joint-grant"]["occupied"], 6600)
        plan, _ = rc.issuance_plan(self.tables, self.slices, as_of)
        status = {(p["evidence_id"], p["program"]): p for p in plan}
        # 当时 E05 的发票（6/24）不存在，按材料不足处理而非 cap_blocked
        self.assertEqual(status[("E05", "bath")]["status"], "missing_document")
        # 全量口径下同一笔业务则是受上限约束
        full, _ = rc.issuance_plan(self.tables, self.slices)
        full_status = {(p["evidence_id"], p["program"]): p for p in full}
        self.assertEqual(full_status[("E05", "bath")]["status"], "cap_blocked")


class LoaderContractTests(unittest.TestCase):
    """新增模块复用既有交换约定，不破坏原始结构检查。"""

    def test_loader_returns_all_collections(self):
        tables = rc.load_tables()
        self.assertEqual(set(tables),
                         {"visits", "expenses", "submitted_invoices",
                          "shares", "funds", "allocations"})

    def test_json_report_serializable(self):
        import json
        report = rc.monthly_close(rc.load_tables(), "2026-06")
        encoded = json.dumps(report, ensure_ascii=False, default=str)
        self.assertIn("issuance_plan", encoded)


if __name__ == "__main__":
    unittest.main()
