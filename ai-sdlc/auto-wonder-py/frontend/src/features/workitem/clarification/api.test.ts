import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from '@/shared/api/client';
import {
  cancelClarificationTurn,
  getClarificationTurnEvents,
  replyClarificationElicitation,
  submitClarificationTurn,
} from './api';

vi.mock('@/shared/api/client', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

const originalCrypto = Object.getOwnPropertyDescriptor(globalThis, 'crypto');

describe('clarification api', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    if (originalCrypto) {
      Object.defineProperty(globalThis, 'crypto', originalCrypto);
    } else {
      Reflect.deleteProperty(globalThis, 'crypto');
    }
  });

  it('submits a UUID when randomUUID is unavailable in an HTTP context', async () => {
    Object.defineProperty(globalThis, 'crypto', {
      configurable: true,
      value: {
        getRandomValues: (bytes: Uint8Array) => {
          bytes.set(Array.from({ length: 16 }, (_, index) => index));
          return bytes;
        },
      },
    });
    vi.mocked(apiClient.post).mockResolvedValue({ data: null } as never);

    await submitClarificationTurn(10011, 10010, '请澄清需求');

    expect(apiClient.post).toHaveBeenCalledWith(
      '/api/workitems/10011/clarification-conversations/10010/turns',
      {
        content: '请澄清需求',
        clientMessageId: '00010203-0405-4607-8809-0a0b0c0d0e0f',
      },
    );
  });

  it('cancels a turn via the per-turn cancel endpoint', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ data: null } as never);

    await cancelClarificationTurn(10011, 10010, 77);

    expect(apiClient.post).toHaveBeenCalledWith(
      '/api/workitems/10011/clarification-conversations/10010/turns/77/cancel',
    );
  });
});

describe('clarification acp api', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('replies to a card on the per-request reply endpoint with a serialised answer', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ data: null } as never);

    await replyClarificationElicitation(10011, 10010, 'a1b2c3', 'accept', { q0: '方案A' });

    expect(apiClient.post).toHaveBeenCalledWith(
      '/api/workitems/10011/clarification-conversations/10010/elicitations/a1b2c3/reply',
      { action: 'accept', content: '{"q0":"方案A"}' },
    );
  });

  it('sends a null content for decline and escapes the requestId', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ data: null } as never);

    await replyClarificationElicitation(10011, 10010, 'a/b', 'decline');

    expect(apiClient.post).toHaveBeenCalledWith(
      '/api/workitems/10011/clarification-conversations/10010/elicitations/a%2Fb/reply',
      { action: 'decline', content: null },
    );
  });

  it('fetches all events of a single turn', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ data: [] } as never);

    await expect(getClarificationTurnEvents(10011, 10010, 77)).resolves.toEqual([]);

    expect(apiClient.get).toHaveBeenCalledWith(
      '/api/workitems/10011/clarification-conversations/10010/turns/77/events',
    );
  });
});
