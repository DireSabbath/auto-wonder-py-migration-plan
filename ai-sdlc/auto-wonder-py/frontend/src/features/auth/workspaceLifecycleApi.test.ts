import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { createElement, type ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { server } from '@/test/mocks/server';
import { useAuthStore } from '@/shared/auth/store';
import { ApiError } from '@/shared/types/common';
import type { PageResult, RecycleBinItem } from '@/shared/types/common';
import {
  RECYCLE_BIN_QUERY_KEY_PREFIX,
  WORKSPACES_QUERY_KEY_PREFIX,
  deleteWorkspace,
  listRecycleBin,
  recycleBinQueryKey,
  restoreWorkspace,
  updateWorkspace,
  useDeleteWorkspace,
  useRecycleBin,
  useRestoreWorkspace,
  useUpdateWorkspace,
} from './workspaceLifecycleApi';

function makeClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

/** Every test needs to spy on one specific client, so the wrapper is built around a given one. */
function wrapperFor(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client }, children);
  };
}

// Distinct from every page/size the tests pass in, so an implementation that fabricates the
// pagination instead of passing the response body through cannot satisfy the assertions.
const page: PageResult<RecycleBinItem> = {
  list: [
    {
      id: 31,
      name: '星云工坊',
      description: '多 Agent 研发协作空间',
      ownerId: 42,
      ownerName: '爱丽丝',
      deletedAt: '2026-09-01T08:30:00.000Z',
      deletedBy: 43,
      deletedByName: '鲍勃',
      restorable: true,
    },
    {
      id: 32,
      name: '重名空间',
      description: null,
      ownerId: null,
      ownerName: null,
      deletedAt: '2026-08-20T02:00:00.000Z',
      deletedBy: null,
      deletedByName: null,
      restorable: false,
    },
  ],
  total: 137,
  pageNum: 4,
  pageSize: 7,
};

let capturedUrl: URL | null = null;
let requestCount = 0;

beforeEach(() => {
  useAuthStore.getState().clear();
  capturedUrl = null;
  requestCount = 0;
});

function mockRecycleBin(body: PageResult<RecycleBinItem> = page) {
  server.use(
    http.get('/api/workspaces/recycle-bin', ({ request }) => {
      requestCount += 1;
      capturedUrl = new URL(request.url);
      return HttpResponse.json({ success: true, code: '0', message: '', data: body, traceId: null });
    }),
  );
}

describe('query keys', () => {
  it('nests the recycle bin under the shared workspaces prefix', () => {
    // Every lifecycle mutation invalidates ['workspaces']; if the recycle bin key did not start
    // with that prefix a delete or restore would leave a stale row on screen.
    expect(WORKSPACES_QUERY_KEY_PREFIX).toEqual(['workspaces']);
    expect(RECYCLE_BIN_QUERY_KEY_PREFIX).toEqual(['workspaces', 'recycle-bin']);
    expect(recycleBinQueryKey('星云', 2, 20)).toEqual(['workspaces', 'recycle-bin', '星云', 2, 20]);
    expect(recycleBinQueryKey('', 1, 20)).toEqual(['workspaces', 'recycle-bin', '', 1, 20]);
  });
});

describe('listRecycleBin', () => {
  it('returns the paginated payload with pageNum and pageSize', async () => {
    mockRecycleBin();

    const result = await listRecycleBin('', 4, 7);

    expect(result).toEqual(page);
    // Guards against a page/size naming regression in the payload contract.
    expect(result).not.toHaveProperty('page');
    expect(result).not.toHaveProperty('size');
    expect(result.list[1].description).toBeNull();
    expect(result.list[1].restorable).toBe(false);
  });

  it('sends page and size and omits keyword when it is empty or blank', async () => {
    mockRecycleBin();

    await listRecycleBin('', 3, 50);
    expect(capturedUrl!.searchParams.get('page')).toBe('3');
    expect(capturedUrl!.searchParams.get('size')).toBe('50');
    expect(capturedUrl!.searchParams.has('keyword')).toBe(false);

    await listRecycleBin('   ', 1, 20);
    expect(capturedUrl!.searchParams.has('keyword')).toBe(false);
  });

  it('trims the keyword before sending it', async () => {
    mockRecycleBin();

    await listRecycleBin('  星云  ', 1, 20);

    // The server matches with LIKE '%keyword%', so padding would silently narrow the result set.
    expect(capturedUrl!.searchParams.get('keyword')).toBe('星云');
  });
});

describe('useRecycleBin', () => {
  it('normalizes the keyword into the key so padded variants share one cache entry', async () => {
    mockRecycleBin();
    const client = makeClient();

    const { result, rerender } = renderHook(
      ({ keyword }) => useRecycleBin(keyword, 1, 20),
      { wrapper: wrapperFor(client), initialProps: { keyword: '星云' } },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(requestCount).toBe(1);

    rerender({ keyword: ' 星云 ' });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    // A cache split would show up here as a second fetch for the padded spelling.
    expect(result.current.isPlaceholderData).toBe(false);
    expect(requestCount).toBe(1);
  });

  it('keeps the previous page on screen while the next keyword is in flight', async () => {
    // Keyword and page are both part of the key, so without an identity placeholder the table
    // would blank out on every keystroke and every page click.
    let releaseSecond: (() => void) | null = null;
    const secondInFlight = new Promise<void>((resolve) => { releaseSecond = resolve; });

    server.use(
      http.get('/api/workspaces/recycle-bin', async ({ request }) => {
        const keyword = new URL(request.url).searchParams.get('keyword');
        if (keyword === '重名') {
          await secondInFlight;
        }
        return HttpResponse.json({
          success: true,
          code: '0',
          message: '',
          traceId: null,
          data: { ...page, list: [{ ...page.list[0], name: `hit-${keyword}` }] },
        });
      }),
    );
    const client = makeClient();

    const { result, rerender } = renderHook(
      ({ keyword }) => useRecycleBin(keyword, 1, 20),
      { wrapper: wrapperFor(client), initialProps: { keyword: '星云' } },
    );

    await waitFor(() => expect(result.current.data?.list[0].name).toBe('hit-星云'));

    rerender({ keyword: '重名' });

    await waitFor(() => expect(result.current.isPlaceholderData).toBe(true));
    expect(result.current.data?.list[0].name).toBe('hit-星云');
    expect(result.current.isLoading).toBe(false);

    releaseSecond!();

    await waitFor(() => expect(result.current.data?.list[0].name).toBe('hit-重名'));
    expect(result.current.isPlaceholderData).toBe(false);
  });
});

describe('updateWorkspace', () => {
  it('PUTs the editable fields plus the optimistic-lock version', async () => {
    let capturedPath: string | null = null;
    let capturedBody: unknown = null;
    server.use(
      http.put('/api/workspaces/:id', async ({ params, request }) => {
        capturedPath = new URL(request.url).pathname;
        capturedBody = await request.json();
        return HttpResponse.json({
          success: true, code: '0', message: '', traceId: null,
          data: { id: Number(params.id), name: '星云工坊 2' },
        });
      }),
    );

    const result = await updateWorkspace({
      id: 31,
      name: '星云工坊 2',
      description: '新的描述',
      background: null,
      version: 7,
    });

    expect(capturedPath).toBe('/api/workspaces/31');
    // F1.5: version is part of the body, not a header, so it survives proxies and is what the
    // server compares against org.version.
    expect(capturedBody).toEqual({
      name: '星云工坊 2',
      description: '新的描述',
      background: null,
      version: 7,
    });
    expect(result).toEqual({ id: 31, name: '星云工坊 2' });
  });

  it('surfaces a version conflict as an ApiError carrying the stable code', async () => {
    server.use(
      http.put('/api/workspaces/:id', () => HttpResponse.json({
        success: false,
        code: '11004',
        message: '工作空间已被其他人修改',
        data: null,
        traceId: 'trace-conflict',
      })),
    );

    await expect(updateWorkspace({ id: 31, name: 'x', version: 7 }))
      .rejects.toMatchObject({ code: '11004', traceId: 'trace-conflict' });
  });
});

describe('deleteWorkspace', () => {
  it('sends a DELETE to the workspace path and returns the deleted row', async () => {
    const methods: string[] = [];
    let capturedPath: string | null = null;
    server.use(
      http.delete('/api/workspaces/:id', ({ params, request }) => {
        methods.push(request.method);
        capturedPath = new URL(request.url).pathname;
        return HttpResponse.json({
          success: true, code: '0', message: '', traceId: null,
          data: { id: Number(params.id), name: '星云工坊' },
        });
      }),
    );

    const result = await deleteWorkspace(31);

    // F2.2: a logical delete behind DELETE, never a POST that could be mistaken for a create.
    expect(methods).toEqual(['DELETE']);
    expect(capturedPath).toBe('/api/workspaces/31');
    expect(result).toEqual({ id: 31, name: '星云工坊' });
  });
});

describe('restoreWorkspace', () => {
  it('POSTs the new name when a rename was requested', async () => {
    let capturedPath: string | null = null;
    let capturedBody: unknown = null;
    server.use(
      http.post('/api/workspaces/:id/restore', async ({ request }) => {
        capturedPath = new URL(request.url).pathname;
        capturedBody = await request.json();
        return HttpResponse.json({
          success: true, code: '0', message: '', traceId: null, data: { id: 32 },
        });
      }),
    );

    await restoreWorkspace({ id: 32, newName: '  重名空间 2  ' });

    expect(capturedPath).toBe('/api/workspaces/32/restore');
    // D4: the trimmed name is sent as-is so the server can rebind active_name_key in one step.
    expect(capturedBody).toEqual({ newName: '重名空间 2' });
  });

  it('POSTs an empty body when no rename was requested', async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post('/api/workspaces/:id/restore', async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({
          success: true, code: '0', message: '', traceId: null, data: { id: 31 },
        });
      }),
    );

    await restoreWorkspace({ id: 31 });
    await restoreWorkspace({ id: 31, newName: null });
    await restoreWorkspace({ id: 31, newName: '   ' });

    // A whitespace-only name is not a rename: sending it would make the server look for an
    // active workspace named '' and fail the restore for no reason.
    expect(bodies).toEqual([{}, {}, {}]);
  });

  it('surfaces the rename-required conflict as an ApiError', async () => {
    server.use(
      http.post('/api/workspaces/:id/restore', () => HttpResponse.json({
        success: false,
        code: '11007',
        message: '已存在同名的在用工作空间',
        data: null,
        traceId: 'trace-name-conflict',
      })),
    );

    await expect(restoreWorkspace({ id: 32 })).rejects.toBeInstanceOf(ApiError);
    await expect(restoreWorkspace({ id: 32 })).rejects.toMatchObject({
      code: '11007',
      message: '已存在同名的在用工作空间',
    });
  });
});

describe('lifecycle mutations invalidate the shared prefix', () => {
  // Each of these moves a row between 我的工作空间, 所有工作空间 and the recycle bin, so all three
  // lists have to be re-read from the ['workspaces'] prefix rather than from one exact key.
  function mockAllLifecycleEndpoints() {
    const ok = () => HttpResponse.json({
      success: true, code: '0', message: '', traceId: null, data: { id: 31 },
    });
    server.use(
      http.put('/api/workspaces/:id', ok),
      http.delete('/api/workspaces/:id', ok),
      http.post('/api/workspaces/:id/restore', ok),
    );
  }

  function renderWithSharedClient<T>(hookFn: () => T) {
    const client = makeClient();
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    const view = renderHook(hookFn, { wrapper: wrapperFor(client) });
    return { ...view, invalidate };
  }

  it('useUpdateWorkspace invalidates ["workspaces"] after a successful save', async () => {
    mockAllLifecycleEndpoints();
    const { result, invalidate } = renderWithSharedClient(() => useUpdateWorkspace());

    await result.current.mutateAsync({ id: 31, name: '星云工坊 2', version: 7 });

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['workspaces'] }));
    invalidate.mockRestore();
  });

  it('useDeleteWorkspace invalidates ["workspaces"] after a successful delete', async () => {
    mockAllLifecycleEndpoints();
    const { result, invalidate } = renderWithSharedClient(() => useDeleteWorkspace());

    await result.current.mutateAsync(31);

    // F2: the row disappears from both lists and reappears in the recycle bin, so one exact-key
    // invalidation would leave a deleted workspace clickable on the select page.
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['workspaces'] }));
    invalidate.mockRestore();
  });

  it('useRestoreWorkspace invalidates ["workspaces"] after a successful restore', async () => {
    mockAllLifecycleEndpoints();
    const { result, invalidate } = renderWithSharedClient(() => useRestoreWorkspace());

    await result.current.mutateAsync({ id: 31, newName: '星云工坊 2' });

    // F5: the restored row must leave the recycle bin page immediately, otherwise the operator
    // can restore it a second time.
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['workspaces'] }));
    invalidate.mockRestore();
  });

  it('does not invalidate when the mutation fails', async () => {
    server.use(
      http.delete('/api/workspaces/:id', () => HttpResponse.json({
        success: false, code: '11006', message: '工作空间不存在或无权操作',
        data: null, traceId: 'trace-denied',
      })),
    );
    const { result, invalidate } = renderWithSharedClient(() => useDeleteWorkspace());

    await expect(result.current.mutateAsync(31)).rejects.toBeInstanceOf(ApiError);

    // Nothing changed server-side, so re-reading every workspace list would only churn.
    expect(invalidate).not.toHaveBeenCalled();
    invalidate.mockRestore();
  });
});
