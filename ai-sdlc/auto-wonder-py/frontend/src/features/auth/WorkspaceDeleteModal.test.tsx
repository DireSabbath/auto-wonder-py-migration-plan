import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { message } from 'antd';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { server } from '@/test/mocks/server';
import type { WorkspaceInfo } from '@/shared/types/common';
import { WorkspaceDeleteModal } from './WorkspaceDeleteModal';

// antd inserts a thin space between exactly two CJK characters in a Button label (`取消` renders
// as `取 消`), so every button lookup goes through a whitespace-tolerant matcher.
function buttonName(label: string) {
  return new RegExp(label.split('').join('\\s*'));
}

const WORKSPACE: WorkspaceInfo = {
  id: 31,
  name: '星云工坊',
  description: '多 Agent 研发协作空间',
  background: null,
  version: 7,
};

function renderModal(workspace: WorkspaceInfo | null) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onClose = vi.fn();
  const onDeleted = vi.fn();
  const view = render(
    <QueryClientProvider client={queryClient}>
      <WorkspaceDeleteModal workspace={workspace} onDeleted={onDeleted} onClose={onClose} />
    </QueryClientProvider>,
  );
  return { ...view, queryClient, onClose, onDeleted };
}

/** Records every DELETE path and answers with a caller-supplied response. */
function mockDelete(responder: (id: number) => Response) {
  const deletedIds: number[] = [];
  server.use(
    http.delete('/api/workspaces/:id', ({ params }) => {
      const id = Number(params.id);
      deletedIds.push(id);
      return responder(id);
    }),
  );
  return deletedIds;
}

function spySuccess() {
  return vi.spyOn(message, 'success').mockImplementation(
    () => undefined as unknown as ReturnType<typeof message.success>,
  );
}

function spyError() {
  return vi.spyOn(message, 'error').mockImplementation(
    () => undefined as unknown as ReturnType<typeof message.error>,
  );
}

describe('WorkspaceDeleteModal', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('stays closed while no workspace is selected', () => {
    renderModal(null);

    expect(screen.queryByTestId('workspace-delete-consequences')).not.toBeInTheDocument();
    expect(screen.queryByText(/删除「/)).not.toBeInTheDocument();
  });

  it('names the workspace and states the three mandated consequences in order', async () => {
    renderModal(WORKSPACE);

    expect(await screen.findByText('删除「星云工坊」')).toBeInTheDocument();
    // F2.1: the confirmation has to say, before anything is sent, that the space goes to the
    // recycle bin, that members lose access, and that nothing is physically destroyed. The order
    // and the exact wording are the requirement, so they are compared as a whole list.
    const consequences = screen.getByTestId('workspace-delete-consequences');
    expect(within(consequences).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      '删除后该工作空间将移入回收站。',
      '成员暂时无法进入或访问该工作空间。',
      '数据不会物理删除，可通过回收站恢复。',
    ]);
    // D6: the dialog is the only place that tells the operator the timers will not come back on
    // their own after a restore.
    expect(screen.getByText('进行中的定时任务与交付会被暂停，恢复后需要手动重新启用。')).toBeInTheDocument();
  });

  it('offers a destructive confirm button rather than a neutral one', async () => {
    renderModal(WORKSPACE);

    const confirm = await screen.findByRole('button', { name: buttonName('确认删除') });
    // antd renamed this class to `ant-btn-color-dangerous` in 5.21 and the lock resolves 5.29, so
    // match the danger marker rather than one exact spelling.
    expect(confirm.className).toMatch(/dangerous/);
    expect(confirm.className).not.toMatch(/disabled/);
  });

  it('deletes the workspace, tells the caller, and closes', async () => {
    const user = userEvent.setup();
    const success = spySuccess();
    const deletedIds = mockDelete((id) => HttpResponse.json({
      success: true, code: '0', message: '', traceId: null, data: { id },
    }));
    const { onClose, onDeleted } = renderModal(WORKSPACE);

    await user.click(await screen.findByRole('button', { name: buttonName('确认删除') }));

    await waitFor(() => expect(deletedIds).toEqual([31]));
    // F2.2: the copy says 回收站, and so does the toast -- the user must not believe the space
    // is gone for good.
    await waitFor(() => expect(success).toHaveBeenCalledWith('工作空间已移入回收站'));
    // The deleted row is handed back so the caller can drop a token binding to it (F6.3).
    expect(onDeleted).toHaveBeenCalledWith(WORKSPACE);
    expect(onClose).toHaveBeenCalledTimes(1);
    success.mockRestore();
  });

  it('surfaces the backend message when the delete is refused and keeps the dialog open', async () => {
    const user = userEvent.setup();
    const error = spyError();
    const deletedIds = mockDelete(() => HttpResponse.json({
      success: false,
      code: '11006',
      message: '工作空间不存在或无权操作',
      data: null,
      traceId: 'trace-11006',
    }));
    const { onClose, onDeleted } = renderModal(WORKSPACE);

    await user.click(await screen.findByRole('button', { name: buttonName('确认删除') }));

    await waitFor(() => expect(deletedIds).toEqual([31]));
    // F8: a caller who is not the owner or an ADMIN gets one uniform message, and the client must
    // not pretend the delete happened.
    await waitFor(() => expect(error).toHaveBeenCalledWith('工作空间不存在或无权操作'));
    expect(onDeleted).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    error.mockRestore();
  });

  it('keeps the dialog open and reports a transport failure', async () => {
    const user = userEvent.setup();
    const error = spyError();
    mockDelete(() => HttpResponse.error());
    const { onDeleted, onClose } = renderModal(WORKSPACE);

    await user.click(await screen.findByRole('button', { name: buttonName('确认删除') }));

    // apiClient wraps a transport failure into an ApiError carrying axios's own wording, so the
    // generic fallback string is not reachable from here. The contract this layer owns is that
    // the operator is told, and that a failure is never mistaken for a successful delete.
    await waitFor(() => expect(error).toHaveBeenCalled());
    expect(error.mock.calls[0][0]).toEqual(expect.any(String));
    expect(onDeleted).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    error.mockRestore();
  });

  it('invalidates the shared workspaces prefix so the list and the recycle bin both re-read', async () => {
    const user = userEvent.setup();
    spySuccess();
    mockDelete((id) => HttpResponse.json({
      success: true, code: '0', message: '', traceId: null, data: { id },
    }));
    const { queryClient } = renderModal(WORKSPACE);
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');

    await user.click(await screen.findByRole('button', { name: buttonName('确认删除') }));

    // A delete moves the row out of 我的工作空间 / 所有工作空间 and into the recycle bin, so the
    // invalidation has to be the shared prefix rather than one exact list key.
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['workspaces'] }));
    invalidate.mockRestore();
  });

  it('closes on cancel without calling the API', async () => {
    const user = userEvent.setup();
    const deletedIds = mockDelete((id) => HttpResponse.json({
      success: true, code: '0', message: '', traceId: null, data: { id },
    }));
    const { onClose, onDeleted } = renderModal(WORKSPACE);

    await user.click(await screen.findByRole('button', { name: buttonName('取消') }));

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onDeleted).not.toHaveBeenCalled();
    expect(deletedIds).toHaveLength(0);
  });
});
