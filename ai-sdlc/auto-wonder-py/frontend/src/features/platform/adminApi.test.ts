import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { server } from '@/test/mocks/server';
import { ApiError } from '@/shared/types/common';
import {
  PLATFORM_ADMIN_CANDIDATES_QUERY_KEY,
  PLATFORM_ADMINS_QUERY_KEY,
  addPlatformAdmin,
  getPlatformAdmins,
  removePlatformAdmin,
  searchPlatformAdminCandidates,
} from './adminApi';

function ok<T>(data: T) {
  return HttpResponse.json({ success: true, code: '0', message: '', traceId: null, data });
}

const roster = {
  admins: [
    {
      userId: 10000,
      username: 'alice',
      nickname: '爱丽丝',
      email: 'alice@example.com',
      active: true,
      self: true,
      removable: false,
      removeDisabledReason: '平台管理员不可移除自己',
    },
    {
      userId: 10001,
      username: 'bob',
      nickname: null,
      email: null,
      active: false,
      self: false,
      removable: true,
      removeDisabledReason: null,
    },
  ],
  canManage: true,
};

describe('adminApi', () => {
  it('pins the query keys the panel invalidates after a write', () => {
    // Both keys are shared with PlatformAdminPanel; a silent rename would leave a stale roster on
    // screen after adding or removing an admin.
    expect(PLATFORM_ADMINS_QUERY_KEY).toEqual(['platform-admins']);
    expect(PLATFORM_ADMIN_CANDIDATES_QUERY_KEY).toEqual(['platform-admin-candidates']);
  });

  it('unwraps the roster and the caller manage permission', async () => {
    let path = '';
    server.use(http.get('/api/platform/admins', ({ request }) => {
      path = new URL(request.url).pathname;
      return ok(roster);
    }));

    await expect(getPlatformAdmins()).resolves.toEqual(roster);
    expect(path).toBe('/api/platform/admins');
  });

  it('forwards the keyword as a query parameter when searching candidates', async () => {
    let search: string | null = null;
    server.use(http.get('/api/platform/admins/candidates', ({ request }) => {
      search = new URL(request.url).searchParams.get('keyword');
      return ok([{ userId: 10002, username: 'carol', nickname: '卡罗', email: 'carol@example.com' }]);
    }));

    const candidates = await searchPlatformAdminCandidates('carol');

    expect(search).toBe('carol');
    expect(candidates).toHaveLength(1);
    expect(candidates[0].userId).toBe(10002);
  });

  it('sends an empty keyword so the backend returns the unfiltered candidate page', async () => {
    let search: string | null = null;
    server.use(http.get('/api/platform/admins/candidates', ({ request }) => {
      search = new URL(request.url).searchParams.get('keyword');
      return ok([]);
    }));

    await searchPlatformAdminCandidates('');

    expect(search).toBe('');
  });

  it('posts the selected user id and returns the refreshed roster', async () => {
    let method = '';
    let path = '';
    let body: unknown = null;
    server.use(http.post('/api/platform/admins', async ({ request }) => {
      method = request.method;
      path = new URL(request.url).pathname;
      body = await request.json();
      return ok(roster);
    }));

    await expect(addPlatformAdmin(10002)).resolves.toEqual(roster);
    expect(method).toBe('POST');
    expect(path).toBe('/api/platform/admins');
    expect(body).toEqual({ userId: 10002 });
  });

  it('deletes by path and returns the refreshed roster', async () => {
    let method = '';
    let path = '';
    server.use(http.delete('/api/platform/admins/:userId', ({ request }) => {
      method = request.method;
      path = new URL(request.url).pathname;
      return ok({ ...roster, admins: [roster.admins[0]] });
    }));

    const result = await removePlatformAdmin(10001);

    expect(method).toBe('DELETE');
    expect(path).toBe('/api/platform/admins/10001');
    expect(result.admins).toHaveLength(1);
  });

  it('surfaces the backend refusal message when removing the last admin', async () => {
    server.use(http.delete('/api/platform/admins/:userId', () => HttpResponse.json({
      success: false,
      code: '31004',
      message: '平台管理员至少保留一名，无法移除最后一名',
      traceId: 'trace-last-admin',
      data: null,
    })));

    // The panel shows this message verbatim, so it must survive the response interceptor rather
    // than being replaced by a generic HTTP error.
    await expect(removePlatformAdmin(10001)).rejects.toMatchObject({
      message: '平台管理员至少保留一名，无法移除最后一名',
    });
    await expect(removePlatformAdmin(10001)).rejects.toBeInstanceOf(ApiError);
  });
});
