import { describe, it, expect, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';

vi.mock('./api', () => ({
  addComment: vi.fn(async () => ({ id: 1 })),
}));

import { useAddComment } from './hooks';

describe('useAddComment cache invalidation', () => {
  it('invalidates delivery-progress after posting a comment', async () => {
    const qc = new QueryClient();
    const key = ['workitem', '42', 'delivery-progress'];
    qc.setQueryData(key, { agents: [] });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useAddComment(), { wrapper });
    await result.current.mutateAsync({
      workitemId: '42',
      contentMd: '@测试工程师 从第二步开始',
      targetAgentIds: [7],
    });

    await waitFor(() => expect(qc.getQueryState(key)?.isInvalidated).toBe(true));
  });
});
