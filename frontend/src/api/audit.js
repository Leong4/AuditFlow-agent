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
