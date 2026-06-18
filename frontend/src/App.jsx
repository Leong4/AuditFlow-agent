import { Outlet } from 'react-router-dom';
import { AuditHistoryProvider } from './context/AuditHistoryContext.jsx';

export default function App() {
  return (
    <AuditHistoryProvider>
      <Outlet />
    </AuditHistoryProvider>
  );
}
