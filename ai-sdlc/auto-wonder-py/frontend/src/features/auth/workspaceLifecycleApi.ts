import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiClient } from '@/shared/api/client';
import type {
  PageResult,
  RecycleBinItem,
  WorkspaceInfo,
} from '@/shared/types/common';

export interface UpdateWorkspaceParams {
  id: number;
  name: string;
  description?: string | null;
  background?: string | null;
  /** Optimistic lock (F1.5): the server rejects the save unless it still matches org.version. */
  version: number;
}

export interface RestoreWorkspaceParams {
  id: number;
  /** D4: rename-on-restore, so a name taken since the delete can be resolved in one request. */
  newName?: string | null;
}

export async function updateWorkspace({
  id,
  name,
  description,
  background,
  version,
}: UpdateWorkspaceParams): Promise<WorkspaceInfo> {
  const resp = await apiClient.put<WorkspaceInfo>(`/api/workspaces/${id}`, {
    name,
    description,
    background,
    version,
  });
  return resp.data;
}

export async function deleteWorkspace(id: number): Promise<WorkspaceInfo> {
  const resp = await apiClient.delete<WorkspaceInfo>(`/api/workspaces/${id}`);
  return resp.data;
}

export async function listRecycleBin(
  keyword: string,
  page: number,
  size: number,
): Promise<PageResult<RecycleBinItem>> {
  const trimmed = keyword.trim();
  const resp = await apiClient.get<PageResult<RecycleBinItem>>('/api/workspaces/recycle-bin', {
    params: { ...(trimmed ? { keyword: trimmed } : {}), page, size },
  });
  return resp.data;
}

export async function restoreWorkspace({
  id,
  newName,
}: RestoreWorkspaceParams): Promise<WorkspaceInfo> {
  const trimmed = newName?.trim();
  const resp = await apiClient.post<WorkspaceInfo>(
    `/api/workspaces/${id}/restore`,
    trimmed ? { newName: trimmed } : {},
  );
  return resp.data;
}

// Shared prefix of myWorkspacesQueryKey / allWorkspacesQueryKey / recycleBinQueryKey: a delete,
// restore or rename moves a row between all three lists, so every one of them must be re-read.
export const WORKSPACES_QUERY_KEY_PREFIX = ['workspaces'] as const;

export const RECYCLE_BIN_QUERY_KEY_PREFIX = ['workspaces', 'recycle-bin'] as const;

export function recycleBinQueryKey(keyword: string, page: number, size: number) {
  return [...RECYCLE_BIN_QUERY_KEY_PREFIX, keyword, page, size] as const;
}

export function useRecycleBin(keyword: string, page: number, size: number) {
  const trimmed = keyword.trim();
  return useQuery({
    queryKey: recycleBinQueryKey(trimmed, page, size),
    queryFn: () => listRecycleBin(trimmed, page, size),
    // Same rationale as useAllWorkspaces: keyword and page are part of the key, so without an
    // identity placeholder the table blanks out on every keystroke and every page click.
    placeholderData: (prev) => prev,
    staleTime: 0,
  });
}

function useWorkspaceListInvalidation() {
  const queryClient = useQueryClient();
  return () => queryClient.invalidateQueries({ queryKey: WORKSPACES_QUERY_KEY_PREFIX });
}

export function useUpdateWorkspace() {
  const invalidate = useWorkspaceListInvalidation();
  return useMutation({
    mutationFn: updateWorkspace,
    // F1.3: the list must show the new name and description as soon as the save succeeds.
    onSuccess: invalidate,
  });
}

export function useDeleteWorkspace() {
  const invalidate = useWorkspaceListInvalidation();
  return useMutation({
    mutationFn: deleteWorkspace,
    onSuccess: invalidate,
  });
}

export function useRestoreWorkspace() {
  const invalidate = useWorkspaceListInvalidation();
  return useMutation({
    mutationFn: restoreWorkspace,
    onSuccess: invalidate,
  });
}
