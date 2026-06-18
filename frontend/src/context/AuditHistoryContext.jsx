import { createContext, useContext, useState } from 'react';
import {
  createHistoryRecord,
  createMockAuditResult,
  presetHistoryResults,
} from '../api/audit.js';

const AuditHistoryContext = createContext(null);

function normalizeHistoryRecord(record) {
  if (
    record.entity &&
    record.timeScope &&
    record.result &&
    typeof record.archived === 'boolean'
  ) {
    return record;
  }

  const entity =
    record.entity ?? record.result?.entity ?? record.title?.split(' · ')[0];
  const timeScope = record.timeScope ?? record.title?.split(' · ')[1] ?? 'Q1 2026';
  const id = record.id ?? record.result?.query_id ?? crypto.randomUUID();
  const status = record.status ?? record.result?.status ?? 'normal';
  const result =
    record.result ??
    createMockAuditResult({
      entity,
      queryId: id,
    });

  return createHistoryRecord({
    archived: Boolean(record.archived),
    entity,
    id,
    result,
    status,
    timeScope,
  });
}

export function AuditHistoryProvider({ children }) {
  const [historyRecords, setHistoryRecords] = useState(() =>
    presetHistoryResults.map(normalizeHistoryRecord),
  );

  const addHistoryResults = (results) => {
    const nextRecords = results.map((result) =>
      normalizeHistoryRecord({
        id: result.query_id,
        result,
        status: result.status,
        title: `${result.entity} · Q1 2026`,
      }),
    );

    setHistoryRecords((current) => {
      const existingIds = new Set(current.map((record) => record.id));
      const uniqueNewRecords = nextRecords.filter(
        (record) => !existingIds.has(record.id),
      );

      return [...uniqueNewRecords, ...current];
    });
  };

  const deleteHistoryRecord = (recordId) => {
    setHistoryRecords((current) =>
      current.filter((record) => record.id !== recordId),
    );
  };

  const setHistoryRecordArchived = (recordId, archived) => {
    setHistoryRecords((current) =>
      current.map((record) =>
        record.id === recordId ? { ...record, archived } : record,
      ),
    );
  };

  return (
    <AuditHistoryContext.Provider
      value={{
        addHistoryResults,
        deleteHistoryRecord,
        historyRecords,
        setHistoryRecordArchived,
      }}
    >
      {children}
    </AuditHistoryContext.Provider>
  );
}

export function useAuditHistory() {
  const context = useContext(AuditHistoryContext);

  if (!context) {
    throw new Error('useAuditHistory must be used within AuditHistoryProvider');
  }

  return context;
}
