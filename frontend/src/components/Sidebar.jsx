import { useNavigate } from 'react-router-dom';
import { createMockAuditResult } from '../api/audit.js';

const connectedAgents = [
  'Router',
  'CRM',
  'ERP',
  'Finance',
  'Reconciliation',
  'Root-Cause',
];

export default function Sidebar({ historyRecords = [], onRecordContextMenu }) {
  const navigate = useNavigate();
  const handleHistoryClick = (item) => {
    const entity = item.entity ?? item.title?.split(' · ')[0];
    const result =
      item.result ??
      (entity
        ? createMockAuditResult({
            entity,
            queryId: item.id,
          })
        : null);

    if (!result) {
      return;
    }

    navigate('/results', {
      state: {
        fromHistory: true,
        results: [result],
      },
    });
  };
  const visibleHistory = historyRecords.filter((item) => !item.archived);

  return (
    <aside className="sidebar" aria-label="AuditFlow navigation">
      <div className="brand">
        <svg
          className="brand-mark"
          viewBox="0 0 40 40"
          aria-hidden="true"
          focusable="false"
        >
          <path d="M20 3 34 10.5v11.8c0 7.2-5.4 12.4-14 14.7C11.4 34.7 6 29.5 6 22.3V10.5L20 3Z" />
          <path d="M14.2 20.4 18.1 24 26.4 15.8" />
        </svg>
        <span>AuditFlow</span>
      </div>

      <section className="sidebar-section history-section">
        <h2>History</h2>
        <div className="history-list">
          {visibleHistory.map((item) => (
            <button
              className="history-item"
              key={item.id}
              type="button"
              onClick={() => handleHistoryClick(item)}
              onContextMenu={(event) => onRecordContextMenu(event, item)}
            >
              <span>{item.title}</span>
              <small className={`history-status history-status-${item.status}`}>
                {item.status}
              </small>
            </button>
          ))}
        </div>
      </section>

      <section className="sidebar-section connected-agents-section">
        <h2>Connected Agents</h2>
        <div className="source-list">
          {connectedAgents.map((agent) => (
            <div className="source-row" key={agent}>
              <span>{agent}</span>
              <span className="source-status">
                <span className="status-dot" aria-hidden="true" />
                Online
              </span>
            </div>
          ))}
        </div>
      </section>
    </aside>
  );
}
