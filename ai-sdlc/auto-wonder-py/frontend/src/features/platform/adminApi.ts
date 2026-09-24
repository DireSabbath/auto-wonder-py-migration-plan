import { apiClient } from '@/shared/api/client';

export interface PlatformAdmin {
  userId: number;
  username: string;
  nickname: string | null;
  email: string | null;
  /** False when the account was deactivated but still holds the platform-admin flag. */
  active: boolean;
  /** True for the caller's own row, which the backend never lets them remove. */
  self: boolean;
  removable: boolean;
  /** Non-null exactly when removable is false, so a disabled button can explain itself. */
  removeDisabledReason: string | null;
}

export interface PlatformAdminCandidate {
  userId: number;
  username: string;
  nickname: string | null;
  email: string | null;
}

export interface PlatformAdminList {
  admins: PlatformAdmin[];
  canManage: boolean;
}

export const PLATFORM_ADMINS_QUERY_KEY = ['platform-admins'] as const;
export const PLATFORM_ADMIN_CANDIDATES_QUERY_KEY = ['platform-admin-candidates'] as const;

const PLATFORM_ADMINS_PATH = '/api/platform/admins';

export async function getPlatformAdmins(): Promise<PlatformAdminList> {
  const resp = await apiClient.get<PlatformAdminList>(PLATFORM_ADMINS_PATH);
  return resp.data;
}

export async function searchPlatformAdminCandidates(keyword: string): Promise<PlatformAdminCandidate[]> {
  const resp = await apiClient.get<PlatformAdminCandidate[]>(`${PLATFORM_ADMINS_PATH}/candidates`, {
    params: { keyword },
  });
  return resp.data;
}

export async function addPlatformAdmin(userId: number): Promise<PlatformAdminList> {
  const resp = await apiClient.post<PlatformAdminList>(PLATFORM_ADMINS_PATH, { userId });
  return resp.data;
}

export async function removePlatformAdmin(userId: number): Promise<PlatformAdminList> {
  const resp = await apiClient.delete<PlatformAdminList>(`${PLATFORM_ADMINS_PATH}/${userId}`);
  return resp.data;
}
