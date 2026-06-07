
## 目前已经实现的功能

目前已经完成了 Reconciliation Agent 的本地核心逻辑。这个版本暂时不负责完整 Band 链路，也不直接处理 Router 到 Reconciliation Agent 的消息传递，只专注于本地 reconciliation 逻辑。

当前输入是三个 system agent 的输出结构：

- `CRMOutput`
    
- `ERPOutput`
    
- `FinanceOutput`
    

输出是：

- `ReconciliationOutput`
    

目前 Reconciliation Agent 主要做这些事情：

1. 检查 CRM、ERP、Finance 三个系统中的关键字段是否缺失。
    
2. 检查 `entity_match` 是否存在，以及 match confidence 是否过低。
    
3. 检查 CRM、ERP、Finance 三边的 `customer_id` 是否一致。
    
4. 检查 CRM、ERP、Finance 三边的 `contract_id` 是否一致。
    
5. 检查 ERP 的 `invoice_id` 和 Finance 的 `invoice_id` 是否一致。
    
6. 比较 CRM、ERP、Finance 三个系统的币种是否一致。
    
7. 支持 FX conversion 场景。  
    如果 CRM / ERP 使用原始发票币种，而 Finance 使用换算后的付款币种，系统会根据 `original_currency_amount` 和 `exchange_rate` 判断是否可以对上。
    
8. 比较 CRM 的 `contract_amount` 和 ERP 的 `invoice_amount`。
    
9. 支持简单的分期付款判断。  
    例如 `payment_terms` 里写了 `40%, 40%, 20%`，并且 `installment_number = 1`，系统会计算第一期应付金额，而不是直接拿合同总额和发票金额硬比。
    
10. 比较 ERP 的 `invoice_amount` 和 Finance 的 adjusted payment。
    
11. adjusted payment 目前按以下方式计算：
    

```text
payment_amount + tax_deduction + bank_fee - refund_amount
```

12. 支持简单日期信号检查。  
    例如付款日期早于发票日期，或 `overdue_days > 0`，可以被记录为 discrepancy。
    
13. 已经接入 `trace.py`。  
    Reconciliation Agent 执行后，会向 trace 里加入一条记录，说明它进行了规则式字段对比，并记录发现了多少 discrepancy。
    
14. 已为 `agent.py` 和 `test_local.py` 补充了中文注释，方便团队成员理解主要逻辑。
    

---

## 当前测试方式

目前用 `test_local.py` 读取 `data/` 目录下的三个 JSON 文件进行本地测试：

- `crm_mock.json`
    
- `erp_mock.json`
    
- `finance_mock.json`
    

测试脚本会按 `case_id` 对齐三边数据，然后构造：

- `CRMOutput`
    
- `ERPOutput`
    
- `FinanceOutput`
    

再调用：

```text
reconcile(crm, erp, finance, trace)
```

最后输出：

- discrepancies
    
- matched fields
    
- trace 信息
    

目前测试数据一共有 14 个完整 cases。

---

## 当前测试结果

当前 14 个 cases 的本地测试结果都符合预期。

### case_001_clean_full_payment

合同金额、发票金额、回款金额完全一致。  
`customer_id`、`contract_id`、`invoice_id`、currency 和 amount 都能对上。

结果：没有 discrepancy。

---

### case_002_clean_installment_first

合同总额和第一期发票金额不同，但符合分期付款条款。

系统根据：

```text
payment_terms = 3 installments: 40%, 40%, 20%
installment_number = 1
```

计算出第一期金额，并与 ERP invoice amount 对比。

结果：没有 discrepancy。

这一点很重要，因为它说明系统没有把正常的分期付款误判成金额异常。

---

### case_003_clean_entity_alias

三个系统里的公司名略有不同，但通过 `entity_match` 可以确认它们指向同一家公司。

例如：

```text
CRM: Greenfield Energy Ltd
ERP: Greenfield Energy
Finance: Greenfield Energy Limited
```

同时 `customer_id`、`contract_id` 和 `invoice_id` 都可以对上。

结果：没有 discrepancy。

---

### case_004_clean_tax_deduction

Finance 实收金额低于 invoice amount，但差额由 `tax_deduction` 解释。

例如：

```text
ERP invoice_amount = 50000
Finance payment_amount = 47500
Finance tax_deduction = 2500
```

系统会按 adjusted payment 判断：

```text
47500 + 2500 = 50000
```

结果：没有 discrepancy。

---

### case_005_clean_usd_fx

这是 USD 币种案例，CRM、ERP、Finance 都使用 USD 主金额。

这里虽然有 `exchange_rate` 字段，但这个 case 的重点不是测试换汇金额，而是确认系统能处理非 GBP 的正常数据。

结果：没有 discrepancy。

---

### case_006_clean_bank_fee

这是银行手续费场景。

例如：

```text
ERP invoice_amount = 50000
Finance payment_amount = 49850
Finance bank_fee = 150
```

系统会按 adjusted payment 判断：

```text
49850 + 150 = 50000
```

结果：没有 discrepancy。

这个 case 说明系统不会把有明确 `bank_fee` 记录的少到账金额误判成异常。

---

### case_007_dirty_invoice_id_mismatch

这是 invoice linking 异常。

ERP 和 Finance 的 invoice ID 不一致：

```text
ERP invoice_id != Finance invoice_id
```

结果：发现 1 个 discrepancy：

```text
erp_invoice_id vs finance_invoice_id
```

这个 case 用来测试系统能否发现付款记录关联到了错误发票。

---

### case_008_dirty_contract_id_mismatch

这是合同 ID 不一致异常。

CRM 和 Finance 使用同一个 contract ID，但 ERP 使用了另一个 contract ID。

结果：发现 1 个 discrepancy：

```text
contract_id across systems
```

这个 case 用来测试系统能否发现合同关联错误。

---

### case_009_dirty_customer_id_mismatch

这是客户 ID 不一致异常。

CRM 和 Finance 使用同一个 customer ID，但 ERP 使用了另一个 customer ID。

结果：发现 1 个 discrepancy：

```text
customer_id across systems
```

这个 case 用来测试系统能否发现客户匹配错误。

---

### case_010_dirty_missing_required_field

这是关键字段缺失异常。

Finance 缺失：

```text
payment_date
```

结果：发现 1 个 discrepancy：

```text
required_fields
```

这个 case 说明系统能识别关键字段缺失，而不是强行判断所有数据都正常。

---

### case_011_dirty_payment_before_invoice

这是日期异常。

Finance 的付款日期早于 ERP 的发票日期：

```text
payment_date < invoice_date
```

结果：发现 1 个 discrepancy：

```text
invoice_date vs payment_date
```

这个 case 用来测试系统能否发现明显的日期顺序问题。

---

### case_012_dirty_amount_mismatch

这是金额无法对账异常。

例如：

```text
ERP invoice_amount = 70000
Finance payment_amount = 65000
tax_deduction = 0
bank_fee = 0
refund_amount = 0
```

系统计算 adjusted payment 后仍然无法与 invoice amount 对上。

结果：发现 1 个 discrepancy：

```text
invoice_amount vs adjusted_payment_amount
```

差额为：

```text
5000
```

---

### case_013_clean_fx_conversion

这是 FX conversion 的 clean case。

CRM / ERP 使用 USD，Finance 使用 GBP。

例如：

```text
ERP invoice_amount = 100000 USD
Finance original_currency_amount = 100000
Finance exchange_rate = 0.79
Finance payment_amount = 79000 GBP
```

系统会判断：

```text
100000 × 0.79 = 79000
```

结果：没有 discrepancy。

这个 case 说明系统不会简单地把 USD / GBP 币种不同直接判断成异常，而是会结合 FX 字段进行判断。

---

### case_014_dirty_fx_amount_mismatch

这是 FX conversion 后金额不一致的 dirty case。

例如：

```text
ERP invoice_amount = 100000 USD
Finance original_currency_amount = 100000
Finance exchange_rate = 0.79
理论换算金额 = 79000 GBP
Finance payment_amount = 76000 GBP
```

系统判断 Finance 实际付款金额低于理论换算金额。

结果：发现 1 个 discrepancy：

```text
fx_converted_amount vs adjusted_payment_amount
```

差额为：

```text
3000
```

---

## 当前结果是否符合预期

目前结果符合这一阶段的预期。

当前已经验证：

- 普通全额付款可以正确识别为 clean。
    
- 分期付款不会被误判成金额异常。
    
- 公司名轻微不同但匹配成功时，不会被误判成异常。
    
- 税款扣除导致的实收金额降低，不会被误判成异常。
    
- 银行手续费导致的实收金额降低，不会被误判成异常。
    
- USD 同币种金额可以正常通过。
    
- FX conversion clean case 不会被误判成 currency mismatch 或 amount mismatch。
    
- invoice ID 不一致可以被发现。
    
- contract ID 不一致可以被发现。
    
- customer ID 不一致可以被发现。
    
- 关键字段缺失可以被发现。
    
- 付款早于发票日期可以被发现。
    
- 无法解释的金额差异可以被发现。
    
- FX conversion 后金额不一致可以被发现。
    

所以当前版本已经不只是 clean baseline，而是可以区分：

```text
clean cases
suspicious-but-clean cases
dirty cases
```

---

## 当前没有做的事情

目前这版还没有做完整的 Band workflow integration。

目前已经完成的是：

```text
CRMOutput + ERPOutput + FinanceOutput
→ reconcile()
→ ReconciliationOutput
```

还没有完成的是：

```text
Band message
→ 解析 Router / System Agents 发来的结构化数据
→ 构造 CRMOutput / ERPOutput / FinanceOutput
→ 调用 reconcile()
→ 返回 ReconciliationOutput 到 Band room
```

这部分属于后续团队链路调试，需要等 Router / System Agents 的 message format 确认之后再接。

另外，目前也还没有处理更复杂的财务场景，例如：

- 多张发票对应一笔付款。
    
- 多笔付款对应一张发票。
    
- payment allocation。
    
- credit note 的完整语义。
    
- 系统同步延迟的时间窗口判断。
    
- 更复杂的 Root-Cause 分析。
    

这些可以后续再扩展。

---

## 后续需要改进的地方

1. 接入 Band workflow。  
    当前版本只是本地 Python 逻辑。后续需要把 Reconciliation Agent 接到 Band room 中，让它能接收 Router / System Agents 的结构化输出并返回 ReconciliationOutput。
    
2. 确认 message format。  
    在接 Band 之前，需要确认 Router 或 System Agents 会以什么格式把 CRMOutput、ERPOutput 和 FinanceOutput 传给 Reconciliation Agent。
    
3. 更完整地处理 payment allocation。  
    当前 schema 暂时没有支持多发票 / 多付款分摊结构。如果后续 demo 需要复杂付款分摊，需要再扩展 schema 和逻辑。
    
4. 更完整地处理 credit note。  
    当前有 `refund_amount`，但 credit note 是否应该作为独立字段处理，需要团队进一步确认。
    
5. 更正式地组织测试文件。  
    当前 `test_local.py` 是本地测试脚本，可以帮助我们在不接 Band 的情况下验证逻辑。后续如果项目时间允许，可以考虑把它放进 `tests/` 文件夹，或者改成更正式的测试文件。
    
6. 与 Root-Cause Agent 对接。  
    当前 Reconciliation Agent 只负责找 matched fields 和 discrepancies，不解释根本原因。具体原因分析应该交给 Root-Cause Agent。
    

---

## 当前结论

当前 Reconciliation Agent 的本地核心逻辑已经跑通，并且已经通过 14 个 mock cases 的本地测试。

当前版本已经可以作为 Reconciliation Agent 的提交版本，主要包括：

```text
agents/reconciliation/agent.py
agents/reconciliation/test_local.py
```

其中：

```text
agent.py
```

负责核心 reconciliation 逻辑。

```text
test_local.py
```

负责读取本地 mock data，按 `case_id` 对齐 CRM、ERP、Finance 三边数据，并调用 `reconcile()` 进行测试。

当前不建议把 `test_band_connection.py` 一起提交，因为它只是本地 Band 连接测试脚本，不属于正式 Reconciliation Agent 逻辑。

---

## Band 连接测试补充

此前已经本地测试过 Reconciliation remote agent 连接。

测试结果：

```text
.env 可以正常读取
agent 可以连上 Band
agent 在 room 里被 @ 后可以回复
```

但当时只是默认聊天回复，还没有接入 Reconciliation Agent 的业务逻辑。

所以目前状态是：

```text
Band remote connection 已测试通过
本地 reconciliation logic 已测试通过
完整 Band workflow integration 尚未完成
```

后续需要等团队链路调试时，把 Band message handling 和本地 `reconcile()` 函数连接起来。