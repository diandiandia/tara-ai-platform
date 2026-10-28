import axios from 'axios';
import { useAuthStore } from './authStore';

const API_BASE = '/api';

const getHeaders = () => {
  const token = useAuthStore.getState().token;
  return token ? { Authorization: `Bearer ${token}` } : {};
};

/** 上传客户（OEM）需求 Excel / CSV */
export async function importCustomerRequirements(projectId, file) {
  const form = new FormData();
  form.append('file', file);
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
