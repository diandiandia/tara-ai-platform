import { useI18n } from './stores/i18nStore';
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
import axios from 'axios';

const isTokenExpired = (token) => {
  if (!token) return true;
  try {
    const base64Url = token.split('.')[1];
    const base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/');
    const jsonPayload = decodeURIComponent(atob(base64).split('').map(function(c) {
      return '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2);
    }).join(''));
    const payload = JSON.parse(jsonPayload);
    if (payload.exp && Date.now() >= payload.exp * 1000) {
      return true;
    }
    return false;
  } catch (e) {
    return true;
  }
};

export default function App() {
  const { t } = useI18n();
  const { token, user, logout } = useAuthStore();
  const [page, setPage] = useState(user?.role === 'admin' ? 'users' : 'projects');
  
  // Navigation states
  const [projectId, setProjectId] = useState(null);
  const [domainId, setDomainId] = useState(null);
  const [diagramId, setDiagramId] = useState(null);

  // Verify session on mount and configure global interceptors to handle token expiration/timeout (BR-01)
  useEffect(() => {
    const verifySession = async () => {
      if (token) {
        if (isTokenExpired(token)) {
          logout();
          alert(t("登录已超时，请重新登录。"));
          return;
        }
        try {
          await axios.get('/api/auth/me', {
            headers: { Authorization: `Bearer ${token}` }
          });
        } catch (err) {
          console.error("Session verification failed, logging out", err);
          logout();
        }
      }
    };
    verifySession();

    // 1. Response interceptor to catch any 401 unauthorized errors (e.g. timeout)
    const responseInterceptor = axios.interceptors.response.use(
      (response) => response,
      (error) => {
        if (error.response && error.response.status === 401) {
          console.warn("Unauthorized request detected (401), logging out...");
          logout();
          alert(t("登录已超时，请重新登录。"));
        }
        return Promise.reject(error);
      }
    );

    // 2. Request interceptor to block outgoing requests if token has expired
    const requestInterceptor = axios.interceptors.request.use(
      (config) => {
        if (token && isTokenExpired(token)) {
          console.warn("Token is expired, blocking request and logging out...");
          logout();
          alert(t("登录已超时，请重新登录。"));
          return Promise.reject(new Error("Token expired"));
        }
        return config;
      },
      (error) => Promise.reject(error)
    );

    // 3. Periodic timer to check token expiration every 5 seconds
    const interval = setInterval(() => {
      if (token && isTokenExpired(token)) {
        console.warn("Token expired during periodic check, logging out...");
        logout();
        alert(t("登录已超时，请重新登录。"));
      }
    }, 5000);

    return () => {
      axios.interceptors.response.eject(responseInterceptor);
      axios.interceptors.request.eject(requestInterceptor);
      clearInterval(interval);
    };
  }, [token, logout, t]);

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
