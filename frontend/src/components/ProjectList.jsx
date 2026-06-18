import { useI18n } from '../stores/i18nStore';
import React, { useEffect, useState, useRef } from 'react';
import { useProjectStore } from '../stores/projectStore';
import { useAuthStore } from '../stores/authStore';
import { Plus, Search, Folder, Calendar, Archive, Trash2, ArrowRight, ShieldAlert } from 'lucide-react';

export default function ProjectList({ setPage, setProjectId }) {
  const { t } = useI18n();
  const { 
    projects, 
    fetchProjects, 
    createProject, 
    deleteProject, 
    archiveProject, 
    unarchiveProject,
    loading, 
    error,
    clearError 
  } = useProjectStore();

  const { user } = useAuthStore();
  const [searchQuery, setSearchQuery] = useState('');
  const [showCreateModal, setShowCreateModal] = useState(false);
  
  // Create Project Form
  const [newProjectName, setNewProjectName] = useState('');
  const [newProjectDesc, setNewProjectDesc] = useState('');
  const [formError, setFormError] = useState('');

  const searchTimeoutRef = useRef(null);

  // Fetch initial project list
  useEffect(() => {
    fetchProjects();
  }, []);

  // Handle Search Input with 300ms debounce (BR-4.2.1)
  const handleSearchChange = (e) => {
    const value = e.target.value;
    setSearchQuery(value);

    if (searchTimeoutRef.current) {
      clearTimeout(searchTimeoutRef.current);
    }

    searchTimeoutRef.current = setTimeout(() => {
      fetchProjects(value);
    }, 300);
  };

  const handleOpenProject = (id) => {
    setProjectId(id);
    setPage('workbench');
  };

  const handleCreateSubmit = async (e) => {
    e.preventDefault();
    setFormError('');

    if (!newProjectName.trim()) {
      setFormError(t('项目名称为必填项'));
      return;
    }
    if (newProjectName.length > 50) {
      setFormError(t('项目名称不能超过 50 个字符'));
      return;
    }
    if (newProjectDesc.length > 200) {
      setFormError(t('项目描述不能超过 200 个字符'));
      return;
    }

    const res = await createProject(newProjectName.trim(), newProjectDesc.trim());
    if (res) {
      setShowCreateModal(false);
      setNewProjectName('');
      setNewProjectDesc('');
    }
  };

  const handleDeleteClick = async (e, id, name) => {
    e.stopPropagation();
    if (window.confirm(t('确定要彻底删除项目 "') + name + t('" 吗？这将会级联清空其下所有域控、功能图、资产和运行分析记录。'))) {
      await deleteProject(id);
    }
  };

  const handleArchiveToggle = async (e, project) => {
    e.stopPropagation();
    if (project.is_archived === 1) {
      if (window.confirm(t('确认要将项目 "') + project.name + t('" 解除归档吗？'))) {
        await unarchiveProject(project.id);
      }
    } else {
      if (window.confirm(t('确认要将项目 "') + project.name + t('" 归档吗？归档后所有内容均变为只读。'))) {
        await archiveProject(project.id);
      }
    }
  };

  const getStatusBadgeClass = (status, is_archived) => {
    if (is_archived === 1) return 'badge-completed';
    switch(status) {
      case 'completed': return 'badge-completed';
      case 'in_progress': return 'badge-progress';
      case 'draft': 
      default: 
        return 'badge-draft';
    }
  };

  const getStatusName = (status, is_archived) => {
    if (is_archived === 1) return t('已归档');
    switch(status) {
      case 'completed': return t('分析完成');
      case 'in_progress': return t('进行中');
      case 'draft': 
      default: 
        return t('草稿');
    }
  };

  const formatDate = (dateString) => {
    try {
      const date = new Date(dateString);
      return date.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' });
    } catch (e) {
      return dateString;
    }
  };

  return (
    <div className="dashboard-container">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '36px' }}>
        <div>
          <h1 className="section-title">{t("项目列表")}</h1>
          <p style={{ color: 'var(--text-secondary)', fontSize: '14px' }}>
            {t("管理并规划车载子域控网络安全属性与威胁分析")}
          </p>
        </div>

        <button 
          onClick={() => setShowCreateModal(true)} 
          className="btn btn-primary"
        >
          <Plus size={18} />
          <span>{t("创建新项目")}</span>
        </button>
      </div>

      {error && (
        <div style={{
          background: 'rgba(244, 63, 94, 0.1)',
          border: '1px solid rgba(244, 63, 94, 0.3)',
          color: '#e11d48',
          padding: '12px 16px',
          borderRadius: '8px',
          fontSize: '14px',
          marginBottom: '24px',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center'
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <ShieldAlert size={16} />
            <span>{t(error)}</span>
          </div>
          <button onClick={clearError} style={{ background: 'none', border: 'none', color: '#e11d48', cursor: 'pointer' }}>×</button>
        </div>
      )}

      {/* Search Bar (BR-4.2.1) */}
      <div className="glass" style={{
        display: 'flex',
        alignItems: 'center',
        padding: '6px 14px',
        marginBottom: '28px',
        maxWidth: '480px'
      }}>
        <Search size={18} style={{ color: 'var(--text-muted)', marginRight: '10px' }} />
        <input
          type="text"
          placeholder={t("搜索项目名称或描述...")}
          value={searchQuery}
          onChange={handleSearchChange}
          style={{
            background: 'none',
            border: 'none',
            outline: 'none',
            color: 'var(--text-primary)',
            fontSize: '14px',
            width: '100%'
          }}
        />
      </div>

      {loading && projects.length === 0 ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', padding: '40px 0' }}>
          <div className="spinner"></div>
          <span style={{ color: 'var(--text-secondary)' }}>{t("正在加载项目...")}</span>
        </div>
      ) : projects.length === 0 ? (
        <div className="glass" style={{ padding: '60px 40px', textAlign: 'center', borderStyle: 'dashed' }}>
          <Folder size={48} style={{ color: 'var(--text-muted)', marginBottom: '16px' }} />
          <h3 style={{ fontSize: '18px', marginBottom: '8px' }}>{t("暂无匹配的项目")}</h3>
          <p style={{ color: 'var(--text-secondary)', fontSize: '14px' }}>
            {t("没有找到符合条件的项目，请点击“创建新项目”开始。")}
          </p>
        </div>
      ) : (
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))',
          gap: '24px'
        }}>
          {projects.map((project) => (
            <div 
              key={project.id} 
              className="glass-interactive view-card"
              onClick={() => handleOpenProject(project.id)}
              style={{ 
                cursor: 'pointer',
                display: 'flex',
                flexDirection: 'column',
                minHeight: '220px',
                position: 'relative'
              }}
            >
              {/* Card Header */}
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '14px' }}>
                <span className={`badge ${getStatusBadgeClass(project.status, project.is_archived)}`}>
                  {getStatusName(project.status, project.is_archived)}
                </span>
                <span style={{ 
                  display: 'flex', 
                  alignItems: 'center', 
                  gap: '6px', 
                  fontSize: '12px', 
                  color: 'var(--text-secondary)' 
                }}>
                  <Calendar size={13} />
                  {formatDate(project.created_at)}
                </span>
              </div>

              {/* Title & Description */}
              <h3 style={{ fontSize: '18px', fontWeight: '600', marginBottom: '8px', color: 'var(--text-primary)' }}>
                {project.name}
              </h3>
              <p style={{ 
                color: 'var(--text-secondary)', 
                fontSize: '13px', 
                lineHeight: '1.5',
                marginBottom: '20px',
                flexGrow: 1,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                display: '-webkit-box',
                WebkitLineClamp: 3,
                WebkitBoxOrient: 'vertical'
              }}>
                {project.description || t('暂无描述信息')}
              </p>

              {/* Card Footer Actions */}
              <div style={{ 
                display: 'flex', 
                justifyContent: 'space-between', 
                alignItems: 'center',
                borderTop: '1px solid var(--border-color)',
                paddingTop: '14px',
                marginTop: 'auto'
              }}>
                <span style={{ 
                  display: 'flex', 
                  alignItems: 'center', 
                  gap: '4px', 
                  fontSize: '13px', 
                  fontWeight: '600',
                  color: 'var(--primary)' 
                }}>
                  {t("进入工作台")} <ArrowRight size={14} />
                </span>

                <div style={{ display: 'flex', gap: '8px' }} onClick={(e) => e.stopPropagation()}>
                  {user?.role === 'admin' && (
                    <button
                      onClick={(e) => handleArchiveToggle(e, project)}
                      className="btn btn-secondary"
                      style={{ padding: '6px 10px' }}
                      title={project.is_archived === 1 ? t('解除归档') : t('归档锁定')}
                    >
                      <Archive size={14} style={{ color: project.is_archived === 1 ? 'var(--success)' : 'var(--text-secondary)' }} />
                    </button>
                  )}
                  {project.is_archived !== 1 && (
                    <button
                      onClick={(e) => handleDeleteClick(e, project.id, project.name)}
                      className="btn btn-danger"
                      style={{ padding: '6px 10px', background: 'rgba(244, 63, 94, 0.05)' }}
                      title={t("删除项目")}
                    >
                      <Trash2 size={14} />
                    </button>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Create Modal (BR-4.2.1) */}
      {showCreateModal && (
        <div className="modal-overlay">
          <div className="modal-content glass">
            <h3 style={{ fontSize: '18px', fontWeight: '600', marginBottom: '20px', color: 'var(--text-primary)' }}>
              {t("创建新分析项目")}
            </h3>

            {formError && (
              <div style={{
                background: 'rgba(244, 63, 94, 0.1)',
                border: '1px solid rgba(244, 63, 94, 0.3)',
                color: '#e11d48',
                padding: '10px 12px',
                borderRadius: '6px',
                fontSize: '13px',
                marginBottom: '16px'
              }}>
                {formError}
              </div>
            )}

            <form onSubmit={handleCreateSubmit}>
              <div className="input-group">
                <span className="input-label">{t("项目名称")} <span style={{ color: 'var(--accent)' }}>*</span> ({t("最多50字")})</span>
                <input
                  type="text"
                  className="input-field"
                  placeholder={t("例如: 智能网联车载娱乐系统TARA评估")}
                  value={newProjectName}
                  onChange={(e) => setNewProjectName(e.target.value)}
                  maxLength={50}
                  required
                />
              </div>

              <div className="input-group" style={{ marginBottom: '24px' }}>
                <span className="input-label">{t("项目描述")} ({t("最多200字")})</span>
                <textarea
                  className="input-field"
                  placeholder={t("选填，简要描述该项目的安全范围与边界目标...")}
                  value={newProjectDesc}
                  onChange={(e) => setNewProjectDesc(e.target.value)}
                  maxLength={200}
                  rows={4}
                  style={{ resize: 'none' }}
                />
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '12px' }}>
                <button
                  type="button"
                  onClick={() => setShowCreateModal(false)}
                  className="btn btn-secondary"
                >
                  {t("取消")}
                </button>
                <button
                  type="submit"
                  className="btn btn-primary"
                  disabled={loading}
                >
                  {loading ? t('正在创建...') : t('确认创建')}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
