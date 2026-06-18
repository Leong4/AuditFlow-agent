const MOCK_SCENARIOS = {
  'Acme Corp': {
    status: 'normal',
    system_data: {
      crm: {
        label: 'CRM',
        field: 'contract_amount',
        amount: 120000,
        currency: 'GBP',
      },
      erp: {
        label: 'ERP',
        field: 'invoice_amount',
        amount: 120000,
        currency: 'GBP',
      },
      finance: {
        label: 'Finance',
        field: 'payment_amount',
        amount: 120000,
        currency: 'GBP',
      },
    },
    discrepancies: [],
    root_cause: {
      probable_cause: 'Clean reconciliation',
      evidence: [
        'CRM contract = £120,000',
        'ERP invoice = £120,000',
        'Finance payment = £120,000',
      ],
      recommended_action: 'No action required. All systems are aligned.',
    },
    ai_analysis_text:
      'The reconciliation analysis for Acme Corp has been completed with no discrepancies. The entity is consistent across CRM, ERP, and Finance. The CRM contract amount, ERP invoice amount, and Finance payment amount are all £120,000, so there is no variance to investigate. Overall this is a clean reconciliation with no anomalies. No action is required.',
  },
  'Lakeside Manufacturing': {
    status: 'anomaly',
    system_data: {
      crm: {
        label: 'CRM',
        field: 'contract_amount',
        amount: 70000,
        currency: 'GBP',
      },
      erp: {
        label: 'ERP',
        field: 'invoice_amount',
        amount: 70000,
        currency: 'GBP',
      },
      finance: {
        label: 'Finance',
        field: 'payment_amount',
        amount: 65000,
        currency: 'GBP',
      },
    },
    discrepancies: [
      {
        field_pair: 'invoice_amount vs payment_amount',
        difference: '£5,000',
        direction: 'finance_lower',
      },
    ],
    root_cause: {
      probable_cause: 'Unexplained payment shortfall',
      evidence: [
        'ERP invoice = £70,000',
        'Finance payment = £65,000',
        'No bank fee, tax deduction, or refund on record to explain the £5,000 gap',
      ],
      recommended_action:
        'Escalate to finance team for manual review of the £5,000 shortfall.',
    },
    ai_analysis_text:
      'The reconciliation analysis for Lakeside Manufacturing identified a significant anomaly. The ERP invoice amount of £70,000 does not match the Finance payment of £65,000, leaving a £5,000 discrepancy that cannot be explained by tax deductions, bank fees, or refunds. This is likely an unexplained payment shortfall and represents a high-risk situation. Possible causes include client underpayment, a missing adjustment record, or a manual entry error. Recommended action: review the payment records, check for missing adjustments, and confirm whether the client underpaid. This issue requires human intervention for resolution.',
  },
  'Northbridge Retail': {
    status: 'normal',
    system_data: {
      crm: {
        label: 'CRM',
        field: 'contract_amount',
        amount: 150000,
        currency: 'GBP',
      },
      erp: {
        label: 'ERP',
        field: 'invoice_amount',
        amount: 60000,
        currency: 'GBP',
      },
      finance: {
        label: 'Finance',
        field: 'payment_amount',
        amount: 60000,
        currency: 'GBP',
      },
    },
    discrepancies: [],
    root_cause: {
      probable_cause: 'Installment payment schedule (normal)',
      evidence: [
        'CRM payment terms: 40% / 40% / 20%',
        'First installment 40% of £150,000 = £60,000',
        'ERP invoice and Finance payment both match the expected first installment',
      ],
      recommended_action: 'No action required. Payment is on schedule.',
    },
    ai_analysis_text:
      'The reconciliation analysis for Northbridge Retail has been completed with no discrepancies. The entity is consistent across CRM, ERP, and Finance. The contract amount of £150,000 follows a 40% / 40% / 20% installment schedule, and the first installment of £60,000 matches both the ERP invoice and the Finance payment. Overall this is a successful reconciliation with no anomalies — no action is required as the payment is on schedule.',
  },
  'Greenfield Energy': {
    status: 'normal',
    system_data: {
      crm: {
        label: 'CRM',
        field: 'contract_amount',
        amount: 80000,
        currency: 'GBP',
      },
      erp: {
        label: 'ERP',
        field: 'invoice_amount',
        amount: 80000,
        currency: 'GBP',
      },
      finance: {
        label: 'Finance',
        field: 'payment_amount',
        amount: 80000,
        currency: 'GBP',
      },
    },
    discrepancies: [],
    root_cause: {
      probable_cause: 'Entity alias match (normal)',
      evidence: [
        'CRM entity name = Greenfield Energy Ltd',
        'ERP entity name = Greenfield Energy',
        'Finance entity name = Greenfield Energy Limited',
        'Entity matching aligned Ltd / no suffix / Limited as the same entity',
        'CRM contract, ERP invoice, and Finance payment all equal £80,000',
      ],
      recommended_action:
        'No action required. Entity aliases are aligned and all key fields match.',
    },
    ai_analysis_text:
      'The reconciliation analysis for Greenfield Energy has been completed with no discrepancies. CRM records the entity as Greenfield Energy Ltd, ERP records it as Greenfield Energy, and Finance records it as Greenfield Energy Limited. Entity matching aligns these naming variants to the same customer, and the CRM contract amount, ERP invoice amount, and Finance payment amount are all £80,000 GBP. This is a clean reconciliation with no anomaly.',
  },
  'Silverline Media': {
    status: 'normal',
    system_data: {
      crm: {
        label: 'CRM',
        field: 'contract_amount',
        amount: 50000,
        currency: 'GBP',
      },
      erp: {
        label: 'ERP',
        field: 'invoice_amount',
        amount: 50000,
        currency: 'GBP',
      },
      finance: {
        label: 'Finance',
        field: 'payment_amount',
        amount: 49850,
        currency: 'GBP',
      },
    },
    discrepancies: [],
    root_cause: {
      probable_cause: 'Bank fee adjustment (normal)',
      evidence: [
        'ERP invoice amount = £50,000',
        'Finance payment amount = £49,850',
        'Recorded bank fee = £150',
        'Adjusted Finance amount = £49,850 + £150 = £50,000',
      ],
      recommended_action:
        'No action required. The payment shortfall is fully explained by the recorded bank fee.',
    },
    ai_analysis_text:
      'The reconciliation analysis for Silverline Media has been completed with no discrepancies. The ERP invoice amount is £50,000 GBP and the Finance payment amount is £49,850 GBP, with a recorded bank fee of £150 GBP. After applying the bank fee adjustment, the Finance amount reconciles to £50,000 GBP. The apparent difference is fully explained by the bank fee, so this is a clean reconciliation with no anomaly.',
  },
  'Atlas Software': {
    status: 'normal',
    system_data: {
      crm: {
        label: 'CRM',
        field: 'contract_amount',
        amount: 100000,
        currency: 'USD',
      },
      erp: {
        label: 'ERP',
        field: 'invoice_amount',
        amount: 100000,
        currency: 'USD',
      },
      finance: {
        label: 'Finance',
        field: 'payment_amount',
        amount: 79000,
        currency: 'GBP',
      },
    },
    discrepancies: [],
    root_cause: {
      probable_cause: 'Foreign exchange conversion (normal)',
      evidence: [
        'ERP invoice amount = USD 100,000',
        'Recorded exchange rate = 0.79',
        'Expected GBP value = USD 100,000 × 0.79 = GBP 79,000',
        'Finance payment amount = GBP 79,000',
      ],
      recommended_action:
        'No action required. The USD invoice and GBP payment reconcile at the recorded exchange rate.',
    },
    ai_analysis_text:
      'The reconciliation analysis for Atlas Software has been completed with no discrepancies. ERP records an invoice amount of USD 100,000, while Finance records a payment amount of GBP 79,000. Using the recorded exchange rate of 0.79, USD 100,000 converts to GBP 79,000. This is a valid foreign exchange conversion and not a currency mismatch. The reconciliation is clean with no anomaly.',
  },
  'Harbor Logistics': {
    status: 'anomaly',
    system_data: {
      crm: {
        label: 'CRM',
        field: 'contract_amount',
        amount: 60000,
        currency: 'GBP',
      },
      erp: {
        label: 'ERP',
        field: 'invoice_amount',
        amount: 60000,
        currency: 'GBP',
      },
      finance: {
        label: 'Finance',
        field: 'payment_amount',
        amount: 60000,
        currency: 'GBP',
      },
    },
    discrepancies: [
      {
        field_pair: 'erp_invoice_id vs finance_invoice_id',
        difference: 'INV-HBL-2026-Q1-001 vs INV-HBL-2026-Q1-999',
        direction: 'invoice_id_mismatch',
      },
    ],
    root_cause: {
      probable_cause: 'High Risk anomaly: invoice linkage mismatch',
      evidence: [
        'ERP invoice_id = INV-HBL-2026-Q1-001',
        'Finance invoice_id = INV-HBL-2026-Q1-999',
        'CRM contract, ERP invoice, and Finance payment all equal £60,000',
        'Amount match does not resolve the invoice ID mismatch',
      ],
      recommended_action:
        'High risk. Verify whether the Finance payment was linked to the wrong ERP invoice before approving the reconciliation.',
    },
    ai_analysis_text:
      'The reconciliation analysis for Harbor Logistics identified a High Risk anomaly. The CRM contract amount, ERP invoice amount, and Finance payment amount all match at £60,000 GBP, but the ERP invoice ID is INV-HBL-2026-Q1-001 while the Finance payment is linked to INV-HBL-2026-Q1-999. This invoice ID mismatch suggests the Finance payment may have been associated with the wrong ERP invoice. Recommended action: investigate the invoice-payment linkage and confirm the correct ERP invoice before closing the audit.',
  },
};

const API_BASE = 'http://localhost:8000';
const USE_MOCK_API =
  import.meta.env.PROD || import.meta.env.VITE_USE_MOCK_API === 'true';
const mockSessions = new Map();
const mockResults = new Map();

function getScenario(entity) {
  return MOCK_SCENARIOS[entity] ?? MOCK_SCENARIOS['Northbridge Retail'];
}

function extractEntityName(query) {
  const reconcileMatch = query.match(/reconcile\s+(.+?)\s+for\s+/i);
  if (reconcileMatch?.[1]) {
    return reconcileMatch[1].trim();
  }

  return query;
}

function createMockAuditSession(queries) {
  const auditSessionId = `mock_session_${crypto.randomUUID()}`;
  const sessionQueries = queries.map((query, index) => {
    const entity = extractEntityName(query);
    const queryId = `mock_query_${index + 1}_${crypto.randomUUID()}`;
    const result = createMockAuditResult({
      entity,
      query,
      queryId,
    });

    mockResults.set(queryId, result);

    return {
      query_id: queryId,
      query_text: query,
      status: 'done',
    };
  });

  const session = {
    audit_session_id: auditSessionId,
    queries: sessionQueries,
    room_id: `mock_room_${crypto.randomUUID()}`,
  };

  mockSessions.set(auditSessionId, session);

  return session;
}

export function createMockAuditResult({
  entity,
  query = `Reconcile ${entity} for Q1 2026`,
  queryId,
}) {
  return {
    ...getScenario(entity),
    entity,
    query,
    query_id: queryId,
  };
}

export function createHistoryRecord({
  archived = false,
  entity,
  id,
  result,
  status,
  timeScope = 'Q1 2026',
}) {
  return {
    archived,
    entity,
    id,
    result,
    status,
    timeScope,
    title: `${entity} · ${timeScope}`,
  };
}

// Demo history is preloaded with complete result objects. In production this
// should come from actual completed user query records.
export const presetHistoryResults = [
  createHistoryRecord({
    entity: 'Acme Corp',
    id: 'acme-q1',
    result: createMockAuditResult({
      entity: 'Acme Corp',
      queryId: 'audit_acme_q1',
    }),
    status: 'normal',
  }),
  createHistoryRecord({
    entity: 'Lakeside Manufacturing',
    id: 'lakeside-q1',
    result: createMockAuditResult({
      entity: 'Lakeside Manufacturing',
      queryId: 'audit_lakeside_q1',
    }),
    status: 'anomaly',
  }),
];

async function readJson(response) {
  const payload = await response.json().catch(() => ({}));

  if (!response.ok) {
    const message = payload.detail ?? `Request failed with status ${response.status}`;
    const error = new Error(message);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }

  return payload;
}

export async function startAudit(queries) {
  if (USE_MOCK_API) {
    return createMockAuditSession(queries);
  }

  try {
    const response = await fetch(`${API_BASE}/api/queries`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ queries }),
    });

    return readJson(response);
  } catch (error) {
    console.warn('Backend unavailable, using mock audit session:', error);
    return createMockAuditSession(queries);
  }
}

export async function getAuditStatus(auditSessionId) {
  if (USE_MOCK_API) {
    const mockSession = mockSessions.get(auditSessionId);

    if (mockSession) {
      return mockSession;
    }
  }

  try {
    const response = await fetch(`${API_BASE}/api/queries/${auditSessionId}`);
    return readJson(response);
  } catch (error) {
    const mockSession = mockSessions.get(auditSessionId);

    if (mockSession) {
      return mockSession;
    }

    throw error;
  }
}

export async function getAuditResult(queryId) {
  if (USE_MOCK_API) {
    const mockResult = mockResults.get(queryId);

    if (mockResult) {
      return mockResult;
    }
  }

  try {
    const response = await fetch(`${API_BASE}/api/queries/${queryId}/result`);
    return readJson(response);
  } catch (error) {
    const mockResult = mockResults.get(queryId);

    if (mockResult) {
      return mockResult;
    }

    throw error;
  }
}
