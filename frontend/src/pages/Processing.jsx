import { useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { runAudit } from '../api/audit.js';

const AGENT_STATUS = {
  PENDING: 'pending',
  ACTIVE: 'active',
  DONE: 'done',
};

const AGENT_KEYS = [
  'router',
  'crm',
  'erp',
  'finance',
  'reconciliation',
  'rootcause',
];

const AGENT_LABELS = {
  router: 'Router',
  crm: 'CRM',
  erp: 'ERP',
  finance: 'Finance',
  reconciliation: 'Reconciliation',
  rootcause: 'Root-Cause',
};

const BACKEND_EVENT_NAMES = {
  ROUTER_DISPATCHED: 'router_dispatched',
  SOURCE_AGENTS_RECEIVED: 'source_agents_received',
  RECONCILIATION_COMPLETED: 'reconciliation_completed',
  ROOTCAUSE_COMPLETED: 'rootcause_completed',
};

const PROGRESS_LABELS = {
  audit_created: {
    label: 'Starting...',
    tone: 'running',
  },
  [BACKEND_EVENT_NAMES.ROUTER_DISPATCHED]: {
    label: 'Routing...',
    tone: 'running',
  },
  [BACKEND_EVENT_NAMES.SOURCE_AGENTS_RECEIVED]: {
    label: 'Querying systems...',
    tone: 'running',
  },
  [BACKEND_EVENT_NAMES.RECONCILIATION_COMPLETED]: {
    label: 'Reconciling...',
    tone: 'running',
  },
  [BACKEND_EVENT_NAMES.ROOTCAUSE_COMPLETED]: {
    label: 'Diagnosing...',
    tone: 'running',
  },
  completed: {
    label: '✓ Completed',
    tone: 'complete',
  },
};

// TODO: Replace this simulated flow with backend events from
// GET /api/queries/{query_id}/events or SSE.
const SIMULATED_AGENT_EVENTS = [
  {
    // Future event: Router has created a query_id and mentioned system agents.
    event: BACKEND_EVENT_NAMES.ROUTER_DISPATCHED,
    activeAgents: ['router'],
    doneAgents: ['router'],
    durationMs: 1200,
  },
  {
    // Future events: crm_received, erp_received, and finance_received.
    // The demo groups them because system agents query in parallel.
    event: BACKEND_EVENT_NAMES.SOURCE_AGENTS_RECEIVED,
    activeAgents: ['crm', 'erp', 'finance'],
    doneAgents: ['crm', 'erp', 'finance'],
    durationMs: 1500,
  },
  {
    // Future event: Reconciliation has produced ReconciliationOutput.
    event: BACKEND_EVENT_NAMES.RECONCILIATION_COMPLETED,
    activeAgents: ['reconciliation'],
    doneAgents: ['reconciliation'],
    durationMs: 1200,
  },
  {
    // Future event: Root-Cause has produced final explanation.
    event: BACKEND_EVENT_NAMES.ROOTCAUSE_COMPLETED,
    activeAgents: ['rootcause'],
    doneAgents: ['rootcause'],
    durationMs: 1200,
  },
];

function createInitialStatuses() {
  return Object.fromEntries(
    AGENT_KEYS.map((key) => [key, AGENT_STATUS.PENDING]),
  );
}

function createAuditId() {
  const bytes = new Uint8Array(4);
  crypto.getRandomValues(bytes);
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  return `audit_${hex}`;
}

function extractEntityName(query) {
  const reconcileMatch = query.match(/reconcile\s+(.+?)\s+for\s+/i);
  if (reconcileMatch?.[1]) {
    return reconcileMatch[1].trim();
  }

  return query;
}

function useSimulatedAgentFlow(onComplete) {
  const [agentStatuses, setAgentStatuses] = useState(createInitialStatuses);
  const [currentEvent, setCurrentEvent] = useState('audit_created');

  useEffect(() => {
    const timers = [];
    let elapsedMs = 0;

    setAgentStatuses(createInitialStatuses());
    setCurrentEvent('audit_created');

    SIMULATED_AGENT_EVENTS.forEach((step, index) => {
      const isFinalStep = index === SIMULATED_AGENT_EVENTS.length - 1;

      timers.push(
        window.setTimeout(() => {
          setCurrentEvent(step.event);
          setAgentStatuses((current) => ({
            ...current,
            ...Object.fromEntries(
              step.activeAgents.map((agent) => [agent, AGENT_STATUS.ACTIVE]),
            ),
          }));
        }, elapsedMs),
      );

      elapsedMs += step.durationMs;

      timers.push(
        window.setTimeout(() => {
          setAgentStatuses((current) => ({
            ...current,
            ...Object.fromEntries(
              step.doneAgents.map((agent) => [agent, AGENT_STATUS.DONE]),
            ),
          }));

          if (isFinalStep) {
            setCurrentEvent('completed');
          }
        }, elapsedMs),
      );
    });

    timers.push(window.setTimeout(onComplete, elapsedMs + 800));

    return () => {
      timers.forEach((timer) => window.clearTimeout(timer));
    };
  }, [onComplete]);

  return { agentStatuses, currentEvent };
}

function AgentNode({ agentKey, status }) {
  return (
    <div className={`agent-node agent-node-${status}`}>
      <span>{AGENT_LABELS[agentKey]}</span>
      {status === AGENT_STATUS.DONE && <span className="agent-check">✓</span>}
    </div>
  );
}

function FlowArrow() {
  return (
    <div className="flow-arrow" aria-hidden="true">
      ↓
    </div>
  );
}

function QueryColumn({ audit, agentStatuses, currentEvent }) {
  const progress = PROGRESS_LABELS[currentEvent] ?? PROGRESS_LABELS.audit_created;

  return (
    <article className="processing-card">
      <header className="processing-card-header">
        <div>
          <h2>{audit.entity}</h2>
          <p>{audit.queryId}</p>
        </div>
        <span className={`event-pill event-pill-${progress.tone}`}>
          {progress.label}
        </span>
      </header>

      <div className="agent-flow" aria-label={`${audit.entity} agent workflow`}>
        <div className="flow-layer flow-layer-single">
          <AgentNode agentKey="router" status={agentStatuses.router} />
        </div>
        <FlowArrow />

        <div className="flow-layer flow-layer-systems">
          <div className="system-agent-row">
            <AgentNode agentKey="crm" status={agentStatuses.crm} />
            <AgentNode agentKey="erp" status={agentStatuses.erp} />
            <AgentNode agentKey="finance" status={agentStatuses.finance} />
          </div>
        </div>
        <FlowArrow />

        <div className="flow-layer flow-layer-single">
          <AgentNode
            agentKey="reconciliation"
            status={agentStatuses.reconciliation}
          />
        </div>
        <FlowArrow />

        <div className="flow-layer flow-layer-single">
          <AgentNode agentKey="rootcause" status={agentStatuses.rootcause} />
        </div>
      </div>
    </article>
  );
}

export default function Processing() {
  const location = useLocation();
  const navigate = useNavigate();
  const inputQueries = useMemo(
    () =>
      Array.isArray(location.state?.queries)
        ? location.state.queries.slice(0, 3)
        : ['Reconcile Acme Corp for Q1 2026'],
    [location.state],
  );

  const audits = useMemo(
    () =>
      inputQueries.map((query) => ({
        query,
        entity: extractEntityName(query),
        queryId: createAuditId(),
      })),
    [inputQueries],
  );

  const handleComplete = useCallback(async () => {
    const results = await runAudit(audits);

    navigate('/results', {
      state: {
        audits,
        results,
      },
    });
  }, [audits, navigate]);

  const { agentStatuses, currentEvent } = useSimulatedAgentFlow(handleComplete);

  return (
    <main className="processing-page">
      <header className="processing-hero">
        <div>
          <p className="system-label">Band workflow in progress</p>
          <h1>Running audit...</h1>
          <p>Agents collaborating through Band</p>
        </div>
        <div className="status-summary" aria-label="Processing status">
          <span className="status-dot" aria-hidden="true" />
          Live simulation
        </div>
      </header>

      <section
        className={`processing-grid processing-grid-${audits.length}`}
        aria-label="Parallel audit windows"
      >
        {audits.map((audit) => (
          <QueryColumn
            audit={audit}
            agentStatuses={agentStatuses}
            currentEvent={currentEvent}
            key={audit.queryId}
          />
        ))}
      </section>
    </main>
  );
}
