import type { Squad } from '@/features/squad/api';

const UNSQUADED = '未编队';

export function buildAgentSquadNameMap(
  squads: readonly Pick<Squad, 'name' | 'memberAgentIds'>[],
): Map<number, string[]> {
  const squadNamesByAgentId = new Map<number, string[]>();
  for (const squad of squads) {
    for (const agentId of squad.memberAgentIds ?? []) {
      const names = squadNamesByAgentId.get(agentId);
      if (names) {
        names.push(squad.name);
      } else {
        squadNamesByAgentId.set(agentId, [squad.name]);
      }
    }
  }
  return squadNamesByAgentId;
}

export function formatAgentSquadLabel(
  agentName: string,
  squadNames: readonly string[] | undefined,
): string {
  const suffix = squadNames?.length ? squadNames.join('，') : UNSQUADED;
  return `${agentName}（${suffix}）`;
}
