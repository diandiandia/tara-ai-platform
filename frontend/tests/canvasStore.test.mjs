import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { SourceTextModule, SyntheticModule, createContext } from 'node:vm';
import { create } from 'zustand';

const source = await readFile(new URL('../src/stores/canvasStore.js', import.meta.url), 'utf8');

async function loadStore(axios) {
  const storage = new Map();
  const context = createContext({
    console, AbortController, setTimeout, clearTimeout,
    navigator: { onLine: true },
    window: { location: { protocol: 'http:', host: 'localhost' } },
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key),
    },
  });
  const dependencies = {
    zustand: { create },
    axios: { default: axios },
    './authStore': { useAuthStore: { getState: () => ({ token: null }) } },
  };
  const module = new SourceTextModule(source, { context });
  await module.link(specifier => {
    const exports = dependencies[specifier];
    assert.ok(exports, `Unexpected import: ${specifier}`);
    return new SyntheticModule(Object.keys(exports), function () {
      for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
    }, { context });
  });
  await module.evaluate();
  return module.namespace.useCanvasStore;
}

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

test('pending deletion is guarded across canvas navigation and removed on success', async () => {
  const request = deferred();
  let calls = 0;
  const store = await loadStore({
    delete: (_, options) => {
      calls += 1;
      assert.equal(options.timeout, 30000);
      return request.promise;
    },
  });
  store.setState({ diagrams: [{ id: 1 }] });
  const deletion = store.getState().deleteDiagram(1);
  assert.ok(store.getState().deletingDiagramIds.includes(1));
  store.getState().closeDiagram();
  assert.equal(await store.getState().deleteDiagram(1), false);
  assert.equal(calls, 1);
  request.resolve({});
  assert.equal(await deletion, true);
  assert.equal(store.getState().diagrams.length, 0);
  assert.equal(store.getState().deletingDiagramIds.length, 0);
});

test('failed deletion preserves the diagram and error through a list refresh', async () => {
  const store = await loadStore({
    delete: async () => { throw { response: { status: 403, data: { detail: 'Project is archived' } } }; },
    get: async () => ({ data: [{ id: 1 }] }),
  });
  store.setState({ diagrams: [{ id: 1 }] });
  assert.equal(await store.getState().deleteDiagram(1), false);
  assert.equal(store.getState().diagrams.length, 1);
  await store.getState().fetchDiagrams(1, { skipPrefetch: true });
  assert.equal(store.getState().diagramMutationError, 'Project is archived');
  assert.equal(store.getState().deletingDiagramIds.length, 0);
});

test('a stale list response cannot resurrect a successfully deleted diagram', async () => {
  const request = deferred();
  const store = await loadStore({
    get: () => request.promise,
    delete: async () => ({}),
  });
  store.setState({ diagrams: [{ id: 1 }] });
  const listing = store.getState().fetchDiagrams(1, { skipPrefetch: true });
  assert.equal(await store.getState().deleteDiagram(1), true);
  request.resolve({ data: [{ id: 1 }] });
  await listing;
  assert.equal(store.getState().diagrams.length, 0);
});

test('creation timeout reports an uncertain outcome without automatic retry', async () => {
  let calls = 0;
  const store = await loadStore({
    post: async (_, __, options) => {
      calls += 1;
      assert.equal(options.timeout, 30000);
      throw { code: 'ECONNABORTED' };
    },
  });
  assert.equal(await store.getState().createDiagram(1, 'test'), null);
  assert.match(store.getState().diagramMutationError, /请求超时/);
  assert.equal(calls, 1);
  assert.equal(store.getState().loading, false);
});

test('a stale list response cannot discard a newly created diagram', async () => {
  const request = deferred();
  const store = await loadStore({
    get: () => request.promise,
    post: async () => ({ data: { id: 2, domain_id: 1, title: 'new', version_no: 1 } }),
  });
  store.setState({ diagrams: [{ id: 1 }] });
  const listing = store.getState().fetchDiagrams(1, { skipPrefetch: true });
  await store.getState().createDiagram(1, 'new');
  request.resolve({ data: [{ id: 1 }] });
  await listing;
  assert.ok(store.getState().diagrams.some(diagram => diagram.id === 2));
});

test('delete timeout releases the guard and reports that the outcome needs checking', async () => {
  const store = await loadStore({
    delete: async () => { throw { code: 'ECONNABORTED' }; },
  });
  assert.equal(await store.getState().deleteDiagram(1), false);
  assert.match(store.getState().diagramMutationError, /删除请求超时/);
  assert.equal(store.getState().deletingDiagramIds.length, 0);
});
