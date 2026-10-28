import axios from 'axios';
import { useAuthStore } from './authStore';

const API_BASE = '/api';

const getHeaders = () => {
  const token = useAuthStore.getState().token;
  return token ? { Authorization: `Bearer ${token}` } : {};
};

/** 预览客户（OEM）需求 Excel：返回 sheet 列表、表头、前几行样例 */
export async function previewCustomerRequirements(projectId, file, sheetName) {
  const form = new FormData();
  form.append('file', file);
  if (sheetName) form.append('sheet_name', sheetName);
  const res = await axios.post(
    `${API_BASE}/projects/${projectId}/customer-requirements/preview`,
    form,
    { headers: getHeaders() }
  );
  return res.data;
}

/** 导入客户（OEM）需求；mapping 可显式指定列号，不依赖 Excel 固定格式 */
export async function importCustomerRequirements(projectId, file, mapping = {}) {
  const form = new FormData();
  form.append('file', file);
  if (mapping.sheet_name) form.append('sheet_name', mapping.sheet_name);
  if (mapping.id_column) form.append('id_column', mapping.id_column);
  if (mapping.text_column) form.append('text_column', mapping.text_column);
  if (mapping.section_column) form.append('section_column', mapping.section_column);
  if (mapping.category_column) form.append('category_column', mapping.category_column);
  const res = await axios.post(
    `${API_BASE}/projects/${projectId}/customer-requirements/import`,
    form,
    { headers: getHeaders() }
  );
  return res.data;
}

/** 查询已导入的客户需求 */
export async function fetchCustomerRequirements(projectId) {
  const res = await axios.get(`${API_BASE}/projects/${projectId}/customer-requirements`, {
    headers: getHeaders(),
  });
  return res.data;
}

/** 触发某个域控的最终需求合并（OEM 客户需求 + 本域控 TARA 需求） */
export async function synthesizeMergedRequirements(domainId) {
  const form = new FormData();
  const res = await axios.post(
    `${API_BASE}/domains/${domainId}/merged-requirements/synthesize`,
    form,
    { headers: getHeaders() }
  );
  return res.data;
}

/** 查询某个域控的最终合并需求 */
export async function fetchMergedRequirements(domainId) {
  const res = await axios.get(`${API_BASE}/domains/${domainId}/merged-requirements`, {
    headers: getHeaders(),
  });
  return res.data;
}

/** 下载最终合并需求 Excel（Merged_Requirements sheet） */
export async function exportMergedRequirements(domainId) {
  const res = await axios.get(`${API_BASE}/reports/domains/${domainId}/export`, {
    params: { format: 'xlsx', export_type: 'merged' },
    headers: getHeaders(),
    responseType: 'blob',
  });
  return res.data;
}
