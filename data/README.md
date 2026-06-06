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

## 当前 14 个测试案例

目前共有 14 个完整测试案例。

其中：

```text
case_001 - case_006, case_013 = clean / suspicious-but-clean cases
case_007 - case_012, case_014 = dirty cases
```

也就是说，有些 case 看起来像异常，但实际上可以被业务字段解释清楚，因此不应该被 Reconciliation Agent 误判成真正异常。

---

### case_001_clean_full_payment

这是最简单的 clean case。

含义：

```text
合同金额 = 发票金额 = 回款金额
币种一致
公司名一致
customer_id 一致
contract_id 一致
invoice_id 对应正确
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
这个 case 的重点不是制造汇率异常，而是测试系统能不能处理非 GBP 的数据。

---

### case_006_clean_bank_fee

这是银行手续费的 clean case。

含义：

```text
ERP invoice_amount = 50000
Finance payment_amount = 49850
Finance bank_fee = 150
```

虽然实际到账金额比发票金额少 150，但这个差额已经记录在 `bank_fee` 里。

所以这个 case 不应该被判断为金额异常。

这个 case 用来测试系统能不能理解：

```text
payment_amount + bank_fee = invoice_amount
```

---

### case_007_dirty_invoice_id_mismatch

这是 invoice linking 的 dirty case。

含义：

```text
ERP invoice_id 和 Finance invoice_id 不一致
```

即使金额、客户和合同信息看起来都能对上，付款记录指向了不同的发票 ID，这仍然应该被记录为 discrepancy。

这个 case 用来测试 Reconciliation Agent 能不能发现：

```text
erp.invoice_id != finance.invoice_id
```

---

### case_008_dirty_contract_id_mismatch

这是合同 ID 不一致的 dirty case。

含义：

```text
CRM contract_id = 正确合同
ERP contract_id = 另一个合同
Finance contract_id = 正确合同
```

这种情况说明 ERP 可能关联到了错误合同。

这个 case 用来测试 Reconciliation Agent 能不能发现：

```text
CRM / ERP / Finance 的 contract_id 不一致
```

---

### case_009_dirty_customer_id_mismatch

这是客户 ID 不一致的 dirty case。

含义：

```text
CRM customer_id = 正确客户
ERP customer_id = 另一个客户
Finance customer_id = 正确客户
```

即使公司名看起来一致，只要 customer_id 不一致，也可能意味着系统匹配到了错误客户。

这个 case 用来测试 Reconciliation Agent 能不能发现：

```text
CRM / ERP / Finance 的 customer_id 不一致
```

---

### case_010_dirty_missing_required_field

这是关键字段缺失的 dirty case。

含义：

```text
Finance payment_date 缺失
```

在这个 case 里，金额、客户、合同和发票 ID 都可以对上，但 `payment_date` 是空的。

这个 case 用来测试 Reconciliation Agent 能不能发现关键字段缺失，而不是强行给出完整判断。

---

### case_011_dirty_payment_before_invoice

这是日期异常的 dirty case。

含义：

```text
Finance payment_date 早于 ERP invoice_date
```

在正常业务流程里，付款日期早于发票日期通常是不合理的，至少需要被标记出来。

这个 case 用来测试 Reconciliation Agent 能不能发现日期顺序异常。

---

### case_012_dirty_amount_mismatch

这是金额无法对账的 dirty case。

含义：

```text
ERP invoice_amount = 70000
Finance payment_amount = 65000
tax_deduction = 0
bank_fee = 0
refund_amount = 0
```

也就是说，Finance 少了 5000，而且没有任何已记录的调整字段可以解释这个差额。

这个 case 用来测试 Reconciliation Agent 能不能发现真正的金额差异。

---

### case_013_clean_fx_conversion

这是汇率换算的 clean case。

含义：

```text
CRM 使用 USD
ERP invoice_amount = 100000 USD
Finance payment_amount = 79000 GBP
Finance original_currency_amount = 100000
Finance exchange_rate = 0.79
```

虽然 ERP 和 Finance 的币种不同，但 Finance 记录里保留了原始币种金额和汇率。

这个 case 用来测试系统能不能理解：

```text
original_currency_amount × exchange_rate = payment_amount
100000 × 0.79 = 79000
```

所以这个 case 不应该被判断为 currency mismatch 或 amount mismatch。

---

### case_014_dirty_fx_amount_mismatch

这是汇率换算金额不一致的 dirty case。

含义：

```text
ERP invoice_amount = 100000 USD
Finance original_currency_amount = 100000
Finance exchange_rate = 0.79
理论换算金额 = 79000 GBP
Finance payment_amount = 76000 GBP
```

这里 Finance 的实际付款金额比理论换算金额少 3000，而且没有 tax deduction、bank fee 或 refund 可以解释。

这个 case 用来测试 Reconciliation Agent 能不能发现 FX conversion 后的金额差异。

---

## 当前阶段的数据目标

当前阶段的数据目标已经从“只提供 clean baseline”扩展为：

```text
1. 提供基础 clean cases，用于测试正常流程
2. 提供 suspicious-but-clean cases，用于避免系统误判
3. 提供 dirty cases，用于测试 Reconciliation Agent 是否能发现明确差异
```

这些 mock data 目前主要覆盖以下场景：

```text
普通全额付款
分期付款
公司名 alias
税款扣除
银行手续费
客户 ID 不一致
合同 ID 不一致
发票 ID 不一致
关键字段缺失
日期异常
金额异常
USD / GBP 汇率换算
FX 金额不一致
```

---

## 注意事项

1. 不要随意修改 `payload` 字段名，因为这些字段需要和 `shared/schemas.py` 里的输出结构对接。
2. `metadata` 可以用于测试管理，但最终 agent output 应该主要来自 `payload`。
3. 三个 JSON 文件里的同一个 `case_id` 是一组完整测试案例，不能只改其中一个文件。
4. clean case 不代表所有字段都完全一样；有些 clean case 需要结合业务规则判断，例如分期付款、税款扣除、银行手续费和 FX conversion。
5. dirty case 是故意制造的异常数据，用于测试 Reconciliation Agent 是否能发现差异。
6. 如果后续继续扩展 schema，需要同步更新三个 mock data 文件和本说明文件。
