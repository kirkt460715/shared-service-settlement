# 资料说明

各集合保存在 `data/`，JSON 对象的 `fields` 为字段列表，`key` 为集合主键，`rows` 为记录数组。各记录值均为字符串，空串含义以各集合的说明为准。

## visits

项目活动及实际履约数量，cohort 是可比较的服务对象类别。

主键为 `id`，字段为 `id`、`when`、`provider`、`program`、`group`、`cohort`、`status`、`units`。

## expenses

发生期、成本类型和总额。一项共享凭证可分给多项服务，空路由表示待补证。reversal_for 指被冲销的经济成本凭证，可部分冲销。

主键为 `evidence_id`，字段为 `evidence_id`、`when`、`provider`、`nature`、`amount`、`activity_keys`、`reversal_for`。

## submitted_invoices

部门提交的材料。receipt 指已核实的原始凭证。跨部门提交同 receipt 不是独立付款。

主键为 `id`，字段为 `id`、`dept`、`receipt`、`invoice_at`、`amount`、`state`。

## shares

共同费用协议，生效日版本。权重是同组合一次凭证应归给对应服务的份额。单服务凭证归该服务。按权重分，金额尾差按协议列出的优先项目归集。

主键为 `id`，字段为 `id`、`start`、`group`、`bath_num`、`escort_num`、`denominator`、`remainder_to`。

## allocations

已记账的资金事项。claim 源账；commit、paid、reversed 的金额均为正数，后者取消已支付中的对应金额。

主键为 `id`，字段为 `id`、`fund`、`cost`、`claim`、`transaction`、`amount`、`at`。

## funds

资助额度在同池共用。资格为项目与成本类型的组合。按到账分配额以共享项目上限为准。

主键为 `fund`，字段为 `fund`、`pool`、`pool_cap`、`program`、`nature`、`active_from`、`active_until`。
