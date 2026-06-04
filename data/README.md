# Mock Data 说明

这个文件夹里目前有三个 mock data 文件：

```text
crm_mock.json
erp_mock.json
finance_mock.json
```

这三个文件分别模拟三个业务系统：

```text
CRM      = 合同 / 客户信息
ERP      = 发票 / 交付信息
Finance  = 回款 / 财务记录
```

目前这些数据是给 System Agents 和 Reconciliation Agent 做测试用。

---

## 文件结构

每个 JSON 文件里都有一个 `records` 列表。每条记录分成两部分：

```text
metadata
payload
```

### metadata

`metadata` 是 mock data 自己用的管理信息，主要用于查找、对齐和说明测试案例。

常见字段包括：

```text
case_id
time_scope
case_type
description
```

例如：

```text
case_id: case_001_clean_full_payment
time_scope: Q1 2026
```

三个 JSON 文件里会使用相同的 `case_id`，这样 CRM、ERP、Finance 三边的数据可以对应到同一个测试案例。

### payload

`payload` 是真正模拟系统返回的数据。
System Agents 读取 mock data 后，应该主要使用 `payload` 部分来生成对应的输出对象。

也就是说：

```text
crm_mock.json      的 payload 对应 CRMOutput
erp_mock.json      的 payload 对应 ERPOutput
finance_mock.json  的 payload 对应 FinanceOutput
```

`metadata` 不应该直接进入最终的 agent output。它只是用于 mock data 的管理和测试。

---

## 当前 5 个测试案例

目前这 5 个 case 都是 clean cases，也就是说它们不是故意制造的异常数据。
但其中有些 case 不是简单的“所有金额完全一样”，而是包含一些现实业务中正常存在的情况。

---

### case_001_clean_full_payment

这是最简单的 clean case。

含义：

```text
合同金额 = 发票金额 = 回款金额
币种一致
公司名一致
付款没有逾期
```

这个 case 用来测试系统最基本的流程能不能跑通。

---

### case_002_clean_installment_first

这是分期付款的 clean case。

含义：

```text
CRM 里记录的是完整合同金额
ERP 里记录的是第一期发票金额
Finance 里记录的是第一期付款金额
```

所以这里的合同金额不等于当前发票金额，但这不是异常。
因为合同本来就是分期付款。

这个 case 用来提醒 Reconciliation Agent：不能简单地认为 `contract_amount` 必须永远等于 `invoice_amount`。

---

### case_003_clean_entity_alias

这是公司名称略有不同，但实际指向同一家公司。

例如：

```text
CRM: Greenfield Energy Ltd
ERP: Greenfield Energy
Finance: Greenfield Energy Limited
```

这些名字不完全一样，但通过 `entity_match` 可以看出它们被认为是同一个公司。

这个 case 用来测试 entity matching / name alignment。
也就是说，公司名不同不一定代表数据异常。

---

### case_004_clean_tax_deduction

这是税款扣除的 clean case。

含义：

```text
ERP invoice_amount = 50000
Finance payment_amount = 47500
Finance tax_deduction = 2500
```

虽然实际收到的现金是 47500，不是 50000，但差额 2500 已经记录在 `tax_deduction` 里。

所以这个 case 也不是异常。

这个 case 用来测试系统能不能理解：

```text
payment_amount + tax_deduction = invoice_amount
```

---

### case_005_clean_usd_fx

这是 USD 币种的 clean case。

含义：

```text
CRM 使用 USD
ERP 使用 USD
Finance 也使用 USD
主金额保持一致
Finance 里额外记录了 exchange_rate
```

这里的 `exchange_rate` 只是给后续汇率相关逻辑预留信息。
目前这个 case 的重点不是制造汇率异常，而是测试系统能不能处理非 GBP 的数据。

---

## 当前阶段的数据目标

当前阶段的目标不是做复杂脏数据，而是先提供一组稳定的 clean data，用于测试基础流程：

```text
Router
→ System Agents
→ Reconciliation Agent
→ Root-Cause Agent
```

后续可以再加入 dirty cases，例如：

```text
金额不一致
付款缺失
公司名匹配失败
付款逾期
币种不一致
发票日期和付款日期错位
```

但这些 dirty cases 应该在基础流程跑通之后再加入，避免一开始就让调试变得过于混乱。

---

## 注意事项

1. 不要随意修改 `payload` 字段名，因为这些字段需要和 `shared/schemas.py` 里的输出结构对接。
2. `metadata` 可以用于测试管理，但最终 agent output 应该主要来自 `payload`。
3. 当前 5 个 case 都是 clean cases，但 case 002 和 case 004 需要业务规则才能判断为 clean，不是简单的金额完全相等。
