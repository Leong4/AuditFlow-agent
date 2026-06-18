import { useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { getAuditStatus } from '../api/audit.js';

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
    event: BACKEND_EVENT_NAMES.ROUTER_DISPATCHED,
    activeAgents: ['router'],
    doneAgents: ['router'],
    durationMs: 800,
  },
  {
    event: BACKEND_EVENT_NAMES.SOURCE_AGENTS_RECEIVED,
    activeAgents: ['crm', 'erp', 'finance'],
    doneAgents: ['crm', 'erp', 'finance'],
    durationMs: 900,
    doneOffsetsMs: {
      crm: 0,
      erp: 180,
      finance: 320,
    },
  },
  {
    event: BACKEND_EVENT_NAMES.RECONCILIATION_COMPLETED,
    activeAgents: ['reconciliation'],
    doneAgents: ['reconciliation'],
    durationMs: 2600,
  },
];

function createInitialStatuses() {
  return Object.fromEntries(
    AGENT_KEYS.map((key) => [key, AGENT_STATUS.PENDING]),
  );
}

function createInitialStatusesByQuery(queryCount) {
  return Array.from({ length: queryCount }, () => createInitialStatuses());
}

function updateStatusesForAllQueries(current, update) {
  return current.map((statuses) => ({
    ...statuses,
    ...update,
  }));
}

function extractEntityName(query) {
  const reconcileMatch = query.match(/reconcile\s+(.+?)\s+for\s+/i);
  if (reconcileMatch?.[1]) {
    return reconcileMatch[1].trim();
  }

  return query;
}

function useProcessingFlow({
  auditSessionId,
  onComplete,
  onStatusUpdate,
  queryCount,
}) {
  const [agentStatusesByQuery, setAgentStatusesByQuery] = useState(() =>
    createInitialStatusesByQuery(queryCount),
  );
  const [currentEvents, setCurrentEvents] = useState(() =>
    Array.from({ length: queryCount }, () => 'audit_created'),
  );
  const [waitingLonger, setWaitingLonger] = useState(false);

  useEffect(() => {
    if (!auditSessionId) {
      return undefined;
    }

    const timers = [];
    let elapsedMs = 0;
    let pollTimer = null;
    let stopped = false;
    let completionScheduled = false;
    const startedAt = Date.now();

    setAgentStatusesByQuery(createInitialStatusesByQuery(queryCount));
    setCurrentEvents(Array.from({ length: queryCount }, () => 'audit_created'));
    setWaitingLonger(false);

    SIMULATED_AGENT_EVENTS.forEach((step) => {
      timers.push(
        window.setTimeout(() => {
          setCurrentEvents(Array.from({ length: queryCount }, () => step.event));
          setAgentStatusesByQuery((current) =>
            updateStatusesForAllQueries(
              current,
              Object.fromEntries(
                step.activeAgents.map((agent) => [agent, AGENT_STATUS.ACTIVE]),
              ),
            ),
          );
        }, elapsedMs),
      );

      elapsedMs += step.durationMs;

      step.doneAgents.forEach((agent) => {
        timers.push(
          window.setTimeout(() => {
            setAgentStatusesByQuery((current) =>
              updateStatusesForAllQueries(current, {
                [agent]: AGENT_STATUS.DONE,
              }),
            );
          }, elapsedMs + (step.doneOffsetsMs?.[agent] ?? 0)),
        );
      });
    });

    timers.push(
      window.setTimeout(() => {
        setCurrentEvents(
          Array.from(
            { length: queryCount },
            () => BACKEND_EVENT_NAMES.ROOTCAUSE_COMPLETED,
          ),
        );
        setAgentStatusesByQuery((current) =>
          updateStatusesForAllQueries(current, {
            rootcause: AGENT_STATUS.ACTIVE,
          }),
        );

        const poll = async () => {
          if (stopped) {
            return;
          }

          try {
            const status = await getAuditStatus(auditSessionId);
            onStatusUpdate(status);

            const allDone = status.queries.every((query) => query.status === 'done');
            setAgentStatusesByQuery((current) =>
              current.map((statuses, index) => ({
                ...statuses,
                rootcause:
                  status.queries[index]?.status === 'done'
                    ? AGENT_STATUS.DONE
                    : AGENT_STATUS.ACTIVE,
              })),
            );
            setCurrentEvents((current) =>
              current.map((event, index) =>
                status.queries[index]?.status === 'done'
                  ? 'completed'
                  : BACKEND_EVENT_NAMES.ROOTCAUSE_COMPLETED,
              ),
            );

            if (allDone) {
              if (!completionScheduled) {
                completionScheduled = true;
                window.setTimeout(() => onComplete(status), 7000);
              }
              return;
            }
          } catch (error) {
            console.error('Audit status polling failed:', error);
          }

          if (Date.now() - startedAt > 60000) {
            setWaitingLonger(true);
          }

          pollTimer = window.setTimeout(poll, 2500);
        };

        poll();
      }, elapsedMs),
    );

    return () => {
      stopped = true;
      timers.forEach((timer) => window.clearTimeout(timer));
      if (pollTimer) {
        window.clearTimeout(pollTimer);
      }
    };
  }, [auditSessionId, onComplete, onStatusUpdate, queryCount]);

  return { agentStatusesByQuery, currentEvents, waitingLonger };
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
      Array.isArray(location.state?.inputQueries)
        ? location.state.inputQueries.slice(0, 3)
        : ['Reconcile Acme Corp for Q1 2026'],
    [location.state],
  );
  const auditSessionId = location.state?.auditSessionId;
  const roomId = location.state?.roomId;
  const [queryStatuses, setQueryStatuses] = useState([]);

  const audits = useMemo(
    () =>
      inputQueries.map((query, index) => ({
        query,
        entity: extractEntityName(query),
        queryId: queryStatuses[index]?.query_id ?? 'pending query_id',
      })),
    [inputQueries, queryStatuses],
  );

  const handleStatusUpdate = useCallback((status) => {
    setQueryStatuses(status.queries);
  }, []);

  const handleComplete = useCallback((status) => {
    navigate('/results', {
      state: {
        auditSessionId,
        audits: status.queries.map((query) => ({
          query: query.query_text,
          entity: extractEntityName(query.query_text),
          queryId: query.query_id,
        })),
        queryStatuses: status.queries,
        roomId,
      },
    });
  }, [auditSessionId, navigate, roomId]);

  const { agentStatusesByQuery, currentEvents, waitingLonger } = useProcessingFlow({
    auditSessionId,
    onComplete: handleComplete,
    onStatusUpdate: handleStatusUpdate,
    queryCount: audits.length,
  });

  return (
    <main className="processing-page">
      <header className="processing-hero">
        <div>
          <p className="system-label">Band workflow in progress</p>
          <h1>Running audit...</h1>
          <p>Agents collaborating through Band</p>
          {waitingLonger && (
            <p className="processing-waiting">
              Still waiting for Root-Cause final replies. This can take a little longer.
            </p>
          )}
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
        {audits.map((audit, index) => (
          <QueryColumn
            audit={audit}
            agentStatuses={agentStatusesByQuery[index] ?? createInitialStatuses()}
            currentEvent={currentEvents[index] ?? 'audit_created'}
            key={audit.query}
          />
        ))}
      </section>
    </main>
  );
}
