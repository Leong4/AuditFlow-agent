import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import ContextMenu from '../components/ContextMenu.jsx';
import Sidebar from '../components/Sidebar.jsx';
import { useAuditHistory } from '../context/AuditHistoryContext.jsx';
import { availableCompanies } from '../data/companies.js';

const MAX_QUERIES = 3;

function createQuery() {
  return {
    id: crypto.randomUUID(),
    value: '',
  };
}

function ArchivedAudits({ onRecordContextMenu, records }) {
  const navigate = useNavigate();
  const handleArchivedClick = (record) => {
    navigate('/results', {
      state: {
        fromHistory: true,
        results: [record.result],
      },
    });
  };

  return (
    <section className="archived-panel" aria-labelledby="archived-title">
      <div className="archived-header">
        <div>
          <h2 id="archived-title">Archived Audits</h2>
          <p>Previously completed reconciliations</p>
        </div>
      </div>

      {records.length === 0 ? (
        <p className="archived-empty">No archived audits yet</p>
      ) : (
        <div className="archived-grid">
          {records.map((record) => (
            <button
              className="archived-card"
              key={record.id}
              type="button"
              onClick={() => handleArchivedClick(record)}
              onContextMenu={(event) => onRecordContextMenu(event, record)}
            >
              <span>{record.entity}</span>
              <small>{record.timeScope}</small>
              <strong className={`status-badge status-badge-${record.status}`}>
                {record.status.toUpperCase()}
              </strong>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

export default function Dashboard() {
  const navigate = useNavigate();
  const {
    deleteHistoryRecord,
    historyRecords,
    setHistoryRecordArchived,
  } = useAuditHistory();
  const [queries, setQueries] = useState(() => [createQuery()]);
  const [activeQueryId, setActiveQueryId] = useState(() => queries[0].id);
  const [contextMenu, setContextMenu] = useState(null);

  const addQuery = () => {
    const nextQuery = createQuery();

    setQueries((current) => {
      if (current.length >= MAX_QUERIES) {
        return current;
      }

      return [...current, nextQuery];
    });
    setActiveQueryId(nextQuery.id);
  };

  const removeQuery = (id) => {
    const nextQueries = queries.filter((query) => query.id !== id);

    setQueries(nextQueries);

    if (activeQueryId === id && nextQueries.length > 0) {
      setActiveQueryId(nextQueries[nextQueries.length - 1].id);
    }
  };

  const updateQuery = (id, value) => {
    setQueries((current) =>
      current.map((query) => (query.id === id ? { ...query, value } : query)),
    );
  };

  const runAudit = () => {
    const nonEmptyQueries = queries
      .map((query) => query.value.trim())
      .filter(Boolean);

    console.log('AuditFlow queries:', nonEmptyQueries);

    if (nonEmptyQueries.length === 0) {
      return;
    }

    navigate('/processing', {
      state: {
        queries: nonEmptyQueries,
      },
    });
  };

  const fillCompanyQuery = (company) => {
    setQueries((current) =>
      current.map((query) =>
        query.id === activeQueryId
          ? { ...query, value: `Reconcile ${company} for Q1 2026` }
          : query,
      ),
    );
  };

  const openRecordContextMenu = (event, record) => {
    event.preventDefault();
    setContextMenu({
      position: {
        x: event.clientX,
        y: event.clientY,
      },
      recordId: record.id,
    });
  };

  const deleteRecord = (recordId) => {
    deleteHistoryRecord(recordId);
  };

  const setRecordArchived = (recordId, archived) => {
    setHistoryRecordArchived(recordId, archived);
  };

  const canAddQuery = queries.length < MAX_QUERIES;
  const archivedRecords = historyRecords.filter((record) => record.archived);
  const selectedRecord = contextMenu
    ? historyRecords.find((record) => record.id === contextMenu.recordId)
    : null;
  const contextMenuOptions = selectedRecord
    ? [
        {
          action: () => deleteRecord(selectedRecord.id),
          danger: true,
          label: 'Delete',
        },
        {
          action: () =>
            setRecordArchived(selectedRecord.id, !selectedRecord.archived),
          label: selectedRecord.archived ? 'Unarchive' : 'Archive',
        },
      ]
    : [];

  return (
    <div className="dashboard-shell">
      <Sidebar
        historyRecords={historyRecords}
        onRecordContextMenu={openRecordContextMenu}
      />

      <main className="main-panel">
        <header className="hero">
          <div>
            <p className="system-label">Multi-agent audit workspace</p>
            <h1>Audit Reconciliation</h1>
            <p>
              Multi-agent reconciliation across CRM, ERP, and Finance
            </p>
          </div>
          <div className="status-summary" aria-label="System status">
            <span className="status-dot" aria-hidden="true" />
            All sources online
          </div>
        </header>

        <div className="dashboard-top-grid">
          <section className="query-panel" aria-labelledby="query-panel-title">
            <div className="query-panel-header">
              <div>
                <h2 id="query-panel-title">Start audit</h2>
                <p>
                  Each query runs as an isolated parallel audit (separate query_id).
                </p>
              </div>
              <span className="query-count">{queries.length}/{MAX_QUERIES}</span>
            </div>

            <div className="query-list">
              {queries.map((query, index) => (
                <div className="query-row" key={query.id}>
                  <label htmlFor={`query-${query.id}`}>
                    Query {index + 1}
                  </label>
                  <input
                    id={`query-${query.id}`}
                    type="text"
                    value={query.value}
                    placeholder="Reconcile Acme Corp for Q1 2026"
                    onFocus={() => setActiveQueryId(query.id)}
                    onChange={(event) => updateQuery(query.id, event.target.value)}
                  />
                  {queries.length > 1 && (
                    <button
                      className="remove-query"
                      type="button"
                      aria-label={`Remove query ${index + 1}`}
                      onClick={() => removeQuery(query.id)}
                    >
                      ×
                    </button>
                  )}
                </div>
              ))}
            </div>

            <div className="query-actions">
              <button
                className="secondary-action"
                type="button"
                onClick={addQuery}
                disabled={!canAddQuery}
              >
                + Add parallel query
              </button>
              <button className="primary-action" type="button" onClick={runAudit}>
                Run Audit
              </button>
            </div>
          </section>

          <ArchivedAudits
            records={archivedRecords}
            onRecordContextMenu={openRecordContextMenu}
          />
        </div>

        <section className="companies-panel" aria-labelledby="companies-title">
          <div className="companies-header">
            <h2 id="companies-title">Available Companies</h2>
            <p>Mock data available for these entities</p>
          </div>

          <div className="company-grid">
            {availableCompanies.map((company) => (
              <button
                className="company-chip"
                key={company}
                type="button"
                onClick={() => fillCompanyQuery(company)}
              >
                {company}
              </button>
            ))}
          </div>
        </section>
      </main>

      <ContextMenu
        onClose={() => setContextMenu(null)}
        options={contextMenuOptions}
        position={contextMenu?.position}
      />
    </div>
  );
}
