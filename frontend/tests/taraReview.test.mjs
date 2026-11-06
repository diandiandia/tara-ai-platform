import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { createContext, SourceTextModule, SyntheticModule } from 'node:vm';
import { create } from 'zustand';
import { transformWithOxc } from 'vite';

const storeSource = await readFile(new URL('../src/stores/taraStore.js', import.meta.url), 'utf8');
const reviewSource = await readFile(new URL('../src/components/TaraResults.jsx', import.meta.url), 'utf8');
const { code } = await transformWithOxc(reviewSource, 'TaraResults.jsx', { jsx: { runtime: 'automatic' } });

async function load(source, dependencies) {
  const context = createContext({ console, setTimeout, clearTimeout });
  const module = new SourceTextModule(source, { context });
  await module.link(name => {
    const values = dependencies[name];
    assert.ok(values, `Unexpected import: ${name}`);
    return new SyntheticModule(Object.keys(values), function () {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value);
    }, { context });
  });
  await module.evaluate();
  return module.namespace;
}

async function loadStore(axios) {
  return (await load(storeSource, {
    zustand: { create }, axios: { default: axios },
    './authStore': { useAuthStore: { getState: () => ({ token: null }) } },
  })).useTaraStore;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function reviewText(state, prepared = false) {
  let stateIndex = 0;
  const module = await load(code, {
    react: {
      useEffect: () => {},
      useState: initial => {
        const index = stateIndex++;
        return [index === 1 && prepared
          ? { results: state.taraResults, assets: state.assets, domainId: 1 }
          : initial, () => {}];
      },
    },
    'react/jsx-runtime': {
      Fragment: 'Fragment',
      jsx: (type, props) => ({ type, props }),
      jsxs: (type, props) => ({ type, props }),
    },
    '../stores/i18nStore': { useI18n: () => ({ t: key => key }) },
    '../stores/taraStore': { useTaraStore: () => state },
    'lucide-react': Object.fromEntries([
      'ArrowLeft', 'ShieldAlert', 'Edit3', 'Download', 'Plus', 'Layers',
      'Save', 'Trash2', 'X', 'ChevronLeft', 'ChevronRight',
    ].map(name => [name, name])),
  });
  function text(node) {
    if (typeof node === 'string') return node;
    if (Array.isArray(node)) return node.map(text).join(' ');
    return node && typeof node === 'object' ? text(node.props?.children) : '';
  }
  return text(module.default({ domainId: 1, setPage: () => {} }));
}

test('assets finishing first cannot turn a pending result into an empty review', async () => {
  const result = deferred();
  const store = await loadStore({
    get: url => url.endsWith('/tara-results') ? result.promise : Promise.resolve({ data: [] }),
  });
  const pending = store.getState().fetchTaraResults(1);
  await store.getState().fetchAssets(1);
  assert.equal(store.getState().loading, false);
  assert.equal(store.getState().resultsLoading, true);
  const text = await reviewText(store.getState());
  assert.ok(text.includes('正在加载评估数据...'));
  assert.ok(text.includes('请稍候，正在检查分析记录'));
  assert.ok(!text.includes('没有找到分析记录'));
  result.resolve({ data: [{ id: 1 }] });
  await pending;
  assert.equal(store.getState().resultsLoading, false);
  assert.equal(store.getState().taraResults.length, 1);
});

test('result failure survives other requests and shows retry instead of empty data', async () => {
  let failed = true;
  const store = await loadStore({
    get: async url => {
      if (url.endsWith('/tara-results') && failed) throw new Error('incomplete response');
      return { data: [] };
    },
  });
  assert.equal(await store.getState().fetchTaraResults(1), null);
  await store.getState().fetchAssets(1);
  assert.equal(store.getState().error, null);
  assert.equal(store.getState().resultsError, '获取 TARA 分析结果失败');
  const text = await reviewText(store.getState());
  assert.ok(text.includes('评估数据加载失败'));
  assert.ok(text.includes('重新加载评估数据'));
  assert.ok(!text.includes('没有找到分析记录'));
  failed = false;
  await store.getState().fetchTaraResults(1);
  assert.equal(store.getState().resultsError, null);
  assert.ok((await reviewText(store.getState(), true)).includes('没有找到分析记录'));
});

test('asset failure also blocks empty-state classification', async () => {
  const store = await loadStore({
    get: async url => {
      if (url.endsWith('/assets')) throw new Error('network');
      return { data: [] };
    },
  });
  await store.getState().fetchAssets(1);
  await store.getState().fetchTaraResults(1);
  const text = await reviewText(store.getState());
  assert.ok(text.includes('获取资产列表失败'));
  assert.ok(!text.includes('没有找到分析记录'));
});

test('old domain results and errors cannot overwrite the current domain', async () => {
  const oldResults = deferred();
  const oldAssets = deferred();
  const store = await loadStore({
    get: url => {
      if (url.includes('/domains/1/')) {
        return url.endsWith('/assets') ? oldAssets.promise : oldResults.promise;
      }
      return Promise.resolve({ data: [{ id: 2 }] });
    },
  });
  const result = store.getState().fetchTaraResults(1);
  const assets = store.getState().fetchAssets(1);
  await store.getState().fetchTaraResults(2);
  await store.getState().fetchAssets(2);
  oldResults.resolve({ data: [{ id: 1 }] });
  oldAssets.reject(new Error('old network failure'));
  await Promise.all([result, assets]);
  assert.equal(store.getState().taraResults[0].id, 2);
  assert.equal(store.getState().assets[0].id, 2);
  assert.equal(store.getState().resultsDomainId, 2);
  assert.equal(store.getState().assetsDomainId, 2);
  assert.equal(store.getState().assetsError, null);
});

test('empty data is shown only after both requests and row preparation succeed', async () => {
  const store = await loadStore({ get: async () => ({ data: [] }) });
  assert.ok(!(await reviewText(store.getState())).includes('没有找到分析记录'));
  await store.getState().fetchTaraResults(1);
  await store.getState().fetchAssets(1);
  assert.ok((await reviewText(store.getState())).includes('正在加载评估数据...'));
  assert.ok((await reviewText(store.getState(), true)).includes('没有找到分析记录'));
});
