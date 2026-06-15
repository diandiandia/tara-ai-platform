import React, { useState, useEffect } from 'react';
import { useAuthStore } from './stores/authStore';
import Login from './components/Login';
import Navbar from './components/Navbar';
import ProjectList from './components/ProjectList';
import Workbench from './components/Workbench';
import DfdEditor from './components/DfdEditor';
import TaraResults from './components/TaraResults';
import Settings from './components/Settings';
import ForceChangePassword from './components/ForceChangePassword';
import UserManagement from './components/UserManagement';

export default function App() {
  const { token, user } = useAuthStore();
  const [page, setPage] = useState(user?.role === 'admin' ? 'users' : 'projects');
  
  // Navigation states
  const [projectId, setProjectId] = useState(null);
  const [domainId, setDomainId] = useState(null);
  const [diagramId, setDiagramId] = useState(null);

  // Sync route path according to role permissions
  useEffect(() => {
    if (user) {
      if (user.role === 'admin') {
        if (page !== 'users' && page !== 'settings') {
          setPage('users');
        }
      } else {
        if (page === 'users') {
          setPage('projects');
        }
      }
    }
  }, [user]);

  // If not authenticated, force login screen
  if (!token || !user) {
    return <Login />;
  }

  // Force password change safety constraint (BR-01)
  if (user.must_change_password) {
    return <ForceChangePassword />;
  }

  // State-based router
  const renderPage = () => {
    switch (page) {
      case 'users':
        return <UserManagement />;
      case 'projects':
        return <ProjectList setPage={setPage} setProjectId={setProjectId} />;
      case 'workbench':
        return (
          <Workbench
            setPage={setPage}
            projectId={projectId}
            setProjectId={setProjectId}
            setDomainId={setDomainId}
            setDiagramId={setDiagramId}
          />
        );
      case 'dfd-editor':
        return (
          <DfdEditor
            setPage={setPage}
            domainId={domainId}
            diagramId={diagramId}
          />
        );
      case 'tara-results':
        return (
          <TaraResults
            setPage={setPage}
            domainId={domainId}
          />
        );
      case 'settings':
        return <Settings setPage={setPage} />;
      default:
        return user.role === 'admin' ? <UserManagement /> : <ProjectList setPage={setPage} setProjectId={setProjectId} />;
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: '100vh', width: '100vw' }}>
      <Navbar setPage={setPage} />
      <div style={{ display: 'flex', flexGrow: 1, flexDirection: 'column', width: '100%' }}>
        {renderPage()}
      </div>
    </div>
  );
}
