import { StrictMode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '@/test/mocks/server';
import * as workitemApi from '../api';
import { DispatchActivityFeed } from './DispatchActivityFeed';

function activityTimeline(dispatchId: number, content: string) {
  return HttpResponse.json({
    success: true,
    code: '0',
    message: '',
    traceId: null,
    data: {
      dispatchId,
      activities: [{
        eventId: `${dispatchId}:1`,
        seq: 1,
        eventTime: '2026-09-02T10:00:00Z',
        eventType: 'agent.message',
        level: 'INFO',
        content,
      }],
    },
  });
}

function deferred<T>() {
  let resolve: (value: T) => void;
  let reject: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return {
    promise,
    resolve: (value: T) => resolve(value),
    reject: (reason?: unknown) => reject(reason),
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('DispatchActivityFeed', () => {
  it('exposes loading state and activity content with semantic status and list roles', async () => {
    const response = deferred<ReturnType<typeof activityTimeline>>();
    server.use(http.get('/api/dispatches/401/runtime-trace/activities', () => response.promise));

    render(<DispatchActivityFeed dispatchId={401} status="SUCCEEDED" />);

    expect(screen.getByRole('status', { name: '正在加载执行详情' })).toBeInTheDocument();

    await act(async () => {
      response.resolve(activityTimeline(401, '已完成的执行消息'));
    });

    const section = await screen.findByRole('region', { name: '执行详情' });
    expect(within(section).getByRole('heading', { name: '执行详情' })).toBeInTheDocument();
    const list = within(section).getByRole('list', { name: '执行详情' });
    expect(within(list).getByText('已完成的执行消息')).not.toHaveAttribute('aria-live');
  });

  it('does not let a previous dispatch response overwrite a newer generation snapshot', async () => {
    const previousResponse = deferred<ReturnType<typeof activityTimeline>>();
    let previousRequests = 0;
    let currentRequests = 0;
    server.use(http.get('/api/dispatches/:dispatchId/runtime-trace/activities', ({ params }) => {
      const dispatchId = Number(params.dispatchId);
      if (dispatchId === 402) {
        previousRequests += 1;
        return previousResponse.promise;
      }
      currentRequests += 1;
      return activityTimeline(403, '当前 generation 的完整快照');
    }));

    const { rerender } = render(<DispatchActivityFeed dispatchId={402} status="RUNNING" />);
    await waitFor(() => expect(previousRequests).toBe(1));

    rerender(<DispatchActivityFeed dispatchId={403} status="RUNNING" />);

    expect(await screen.findByText('当前 generation 的完整快照')).toBeInTheDocument();
    expect(currentRequests).toBe(1);
    expect(screen.queryByText('旧 generation 的过期快照')).not.toBeInTheDocument();

    await act(async () => {
      previousResponse.resolve(activityTimeline(402, '旧 generation 的过期快照'));
    });

    expect(screen.getByText('当前 generation 的完整快照')).toBeInTheDocument();
    expect(screen.queryByText('旧 generation 的过期快照')).not.toBeInTheDocument();
  });

  it('aborts a replaced dispatch request and ignores its rejected stale response', async () => {
    const previousResponse = deferred<Awaited<ReturnType<typeof workitemApi.getRuntimeActivities>>>();
    const currentResponse = deferred<Awaited<ReturnType<typeof workitemApi.getRuntimeActivities>>>();
    const signals: AbortSignal[] = [];
    let requests = 0;
    const getRuntimeActivities = vi.spyOn(workitemApi, 'getRuntimeActivities').mockImplementation(
      ((_dispatchId: number | string, signal?: AbortSignal) => {
        const response = requests++ === 0 ? previousResponse : currentResponse;
        if (signal) signals.push(signal);
        return response.promise;
      }) as typeof workitemApi.getRuntimeActivities,
    );

    const { rerender } = render(<DispatchActivityFeed dispatchId={407} status="RUNNING" />);
    expect(getRuntimeActivities).toHaveBeenCalledTimes(1);

    rerender(<DispatchActivityFeed dispatchId={408} status="RUNNING" />);
    expect(getRuntimeActivities).toHaveBeenCalledTimes(2);
    expect(signals[0]).toMatchObject({ aborted: true });
    expect(signals[1]).toMatchObject({ aborted: false });

    await act(async () => {
      currentResponse.resolve({
        dispatchId: 408,
        activities: [{
          eventId: '408:1',
          seq: 1,
          eventTime: '2026-09-02T10:00:00Z',
          eventType: 'agent.message',
          level: 'INFO',
          content: '新 dispatch 的完整快照',
        }],
      });
    });
    expect(screen.getByText('新 dispatch 的完整快照')).toBeInTheDocument();

    await act(async () => {
      previousResponse.reject(new Error('request aborted'));
    });
    expect(screen.getByText('新 dispatch 的完整快照')).toBeInTheDocument();
    expect(screen.queryByText('执行详情加载失败')).not.toBeInTheDocument();
  });

  it('aborts the pending activity request on unmount', () => {
    const pendingResponse = deferred<Awaited<ReturnType<typeof workitemApi.getRuntimeActivities>>>();
    const signals: AbortSignal[] = [];
    vi.spyOn(workitemApi, 'getRuntimeActivities').mockImplementation(
      ((_dispatchId: number | string, signal?: AbortSignal) => {
        if (signal) signals.push(signal);
        return pendingResponse.promise;
      }) as typeof workitemApi.getRuntimeActivities,
    );

    const { unmount } = render(<DispatchActivityFeed dispatchId={411} status="RUNNING" />);
    expect(signals[0]).toMatchObject({ aborted: false });

    unmount();
    expect(signals[0]).toMatchObject({ aborted: true });
  });

  it('aborts an active request before loading the terminal lifecycle of the same dispatch', async () => {
    const pendingResponse = deferred<Awaited<ReturnType<typeof workitemApi.getRuntimeActivities>>>();
    const signals: AbortSignal[] = [];
    let requests = 0;
    vi.spyOn(workitemApi, 'getRuntimeActivities').mockImplementation(
      ((_dispatchId: number | string, signal?: AbortSignal) => {
        if (signal) signals.push(signal);
        return requests++ === 0 ? pendingResponse.promise : Promise.resolve({ dispatchId: 409, activities: [] });
      }) as typeof workitemApi.getRuntimeActivities,
    );

    const { rerender } = render(<DispatchActivityFeed dispatchId={409} status="RUNNING" />);
    rerender(<DispatchActivityFeed dispatchId={409} status="SUCCEEDED" />);
    await act(async () => {});

    expect(signals[0]).toMatchObject({ aborted: true });
    expect(signals[1]).toMatchObject({ aborted: false });
  });

  it('keeps exactly one valid in-flight request across StrictMode remounts', () => {
    const signals: AbortSignal[] = [];
    vi.spyOn(workitemApi, 'getRuntimeActivities').mockImplementation(
      ((_dispatchId: number | string, signal?: AbortSignal) => {
        if (signal) signals.push(signal);
        return new Promise(() => undefined);
      }) as typeof workitemApi.getRuntimeActivities,
    );

    render(
      <StrictMode>
        <DispatchActivityFeed dispatchId={410} status="SUCCEEDED" />
      </StrictMode>,
    );

    expect(signals).toHaveLength(2);
    expect(signals[0]).toMatchObject({ aborted: true });
    expect(signals[1]).toMatchObject({ aborted: false });
    expect(signals.filter((signal) => !signal.aborted)).toHaveLength(1);
  });

  it('polls active snapshots every two seconds and stops polling after becoming terminal', async () => {
    vi.useFakeTimers();
    try {
      let requests = 0;
      const firstRequest = deferred<void>();
      const secondRequest = deferred<void>();
      const terminalRequest = deferred<void>();
      const firstResponse = deferred<ReturnType<typeof activityTimeline>>();
      const secondResponse = deferred<ReturnType<typeof activityTimeline>>();
      const terminalResponse = deferred<ReturnType<typeof activityTimeline>>();
      const requestSignals = [firstRequest, secondRequest, terminalRequest];
      const responses = [firstResponse, secondResponse, terminalResponse];
      server.use(http.get('/api/dispatches/405/runtime-trace/activities', () => {
        const requestIndex = requests++;
        requestSignals[requestIndex]?.resolve(undefined);
        return responses[requestIndex]?.promise ?? activityTimeline(405, '不应请求的快照');
      }));

      const { rerender } = render(<DispatchActivityFeed dispatchId={405} status="RUNNING" />);
      await firstRequest.promise;
      await act(async () => {
        firstResponse.resolve(activityTimeline(405, '初始完整快照'));
      });
      expect(screen.getByText('初始完整快照')).toBeInTheDocument();
      expect(requests).toBe(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000);
      });
      await secondRequest.promise;
      await act(async () => {
        secondResponse.resolve(activityTimeline(405, '轮询后的完整快照'));
      });

      expect(screen.getByText('轮询后的完整快照')).toBeInTheDocument();
      expect(screen.queryByText('初始完整快照')).not.toBeInTheDocument();
      expect(requests).toBe(2);

      rerender(<DispatchActivityFeed dispatchId={405} status="SUCCEEDED" />);
      await terminalRequest.promise;
      await act(async () => {
        terminalResponse.resolve(activityTimeline(405, '终态完整快照'));
      });
      expect(screen.getByText('终态完整快照')).toBeInTheDocument();
      expect(requests).toBe(3);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(4_000);
      });

      expect(requests).toBe(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it('polls PAUSE_FAILED snapshots every two seconds and stops after cancellation', async () => {
    vi.useFakeTimers();
    try {
      let requests = 0;
      const getRuntimeActivities = vi.spyOn(workitemApi, 'getRuntimeActivities').mockImplementation(() => {
        requests += 1;
        return Promise.resolve({
          dispatchId: 412,
          activities: [{
            eventId: `412:${requests}`,
            seq: requests,
            eventTime: '2026-09-02T10:00:00Z',
            eventType: 'agent.message',
            level: 'INFO' as const,
            content: `完整快照 ${requests}`,
          }],
        });
      });

      const { rerender } = render(<DispatchActivityFeed dispatchId={412} status="PAUSE_FAILED" />);
      await act(async () => {});

      expect(getRuntimeActivities).toHaveBeenCalledTimes(1);
      expect(screen.getByText('完整快照 1')).toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000);
      });

      expect(getRuntimeActivities).toHaveBeenCalledTimes(2);
      expect(screen.getByText('完整快照 2')).toBeInTheDocument();

      rerender(<DispatchActivityFeed dispatchId={412} status="CANCELED" />);
      await act(async () => {});
      expect(getRuntimeActivities).toHaveBeenCalledTimes(3);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(4_000);
      });

      expect(getRuntimeActivities).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it('waits for an active request to settle before starting the next polling cadence', async () => {
    vi.useFakeTimers();
    try {
      let requests = 0;
      const initialResponse = deferred<Awaited<ReturnType<typeof workitemApi.getRuntimeActivities>>>();
      const secondResponse = deferred<Awaited<ReturnType<typeof workitemApi.getRuntimeActivities>>>();
      const getRuntimeActivities = vi.spyOn(workitemApi, 'getRuntimeActivities').mockImplementation(() => {
        requests += 1;
        if (requests === 1) return initialResponse.promise;
        if (requests === 2) return secondResponse.promise;
        return Promise.resolve({ dispatchId: 406, activities: [] });
      });

      const { unmount } = render(<DispatchActivityFeed dispatchId={406} status="RUNNING" />);
      expect(getRuntimeActivities).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_000);
      });
      expect(getRuntimeActivities).toHaveBeenCalledTimes(1);

      await act(async () => {
        initialResponse.resolve({ dispatchId: 406, activities: [] });
      });
      expect(getRuntimeActivities).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_999);
      });
      expect(getRuntimeActivities).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(getRuntimeActivities).toHaveBeenCalledTimes(2);

      await act(async () => {
        secondResponse.resolve({ dispatchId: 406, activities: [] });
      });
      unmount();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_000);
      });
      expect(getRuntimeActivities).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps successful activity content through a terminal refresh failure and retries immediately', async () => {
    let requests = 0;
    server.use(http.get('/api/dispatches/403/runtime-trace/activities', () => {
      requests += 1;
      if (requests === 1) return activityTimeline(403, '已加载的执行消息');
      if (requests === 2) return new HttpResponse(null, { status: 500 });
      return activityTimeline(403, '重试后的执行消息');
    }));

    const { rerender } = render(<DispatchActivityFeed dispatchId={403} status="RUNNING" />);
    expect(await screen.findByText('已加载的执行消息')).toBeInTheDocument();

    rerender(<DispatchActivityFeed dispatchId={403} status="SUCCEEDED" />);
    await waitFor(() => expect(requests).toBe(2));

    await screen.findByText('执行详情加载失败');
    expect(screen.getByText('已加载的执行消息')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('执行详情加载失败');

    fireEvent.click(screen.getByRole('button', { name: '重试执行详情' }));
    expect(await screen.findByText('重试后的执行消息')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('allows a terminal attempt to retry an initial activity load failure', async () => {
    let requests = 0;
    server.use(http.get('/api/dispatches/404/runtime-trace/activities', () => {
      requests += 1;
      return requests === 1
        ? new HttpResponse(null, { status: 500 })
        : activityTimeline(404, '终态重试后的执行消息');
    }));

    render(<DispatchActivityFeed dispatchId={404} status="SUCCEEDED" />);

    await screen.findByText('执行详情加载失败');
    expect(screen.getByRole('alert')).toHaveTextContent('执行详情加载失败');
    fireEvent.click(screen.getByRole('button', { name: '重试执行详情' }));

    expect(await screen.findByText('终态重试后的执行消息')).toBeInTheDocument();
  });
});
