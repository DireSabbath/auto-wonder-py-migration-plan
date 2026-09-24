import { Popover, Typography } from 'antd';
import { ThunderboltOutlined } from '@ant-design/icons';
import type { WorkitemUsageRun, WorkitemUsageSummary } from '@/shared/types/workitem';
import { formatCredits, formatCreditsFixed } from '@/shared/lib/tokenFormat';

const { Text } = Typography;

function runLabel(run: WorkitemUsageRun, index: number): string {
  if (run.label) return run.label;
  const name = run.agentName || (run.agentId != null ? `agent-${run.agentId}` : 'unknown');
  return `${name} run-${run.runIndex ?? index + 1}`;
}

function CreditsBreakdown({ usage }: { usage: WorkitemUsageSummary }) {
  const runs = (usage.runs ?? []).filter((run) => (run.credits ?? 0) > 0);
  return (
    <div data-testid="workitem-credits-breakdown" style={{ minWidth: 200, lineHeight: '1.8' }}>
      {runs.map((run, index) => (
        <div key={`${runLabel(run, index)}-${index}`}>
          <span style={{ fontWeight: 600 }}>{runLabel(run, index)}:</span> {formatCredits(run.credits)} credits
        </div>
      ))}
      <div>
        <span style={{ fontWeight: 600 }}>Total:</span> {formatCreditsFixed(usage.credits)} Credits
      </div>
    </div>
  );
}

interface WorkitemCreditsBadgeProps {
  usage?: WorkitemUsageSummary | null;
}

/** 工单详情页顶部的累计 credits 徽标，hover 展开按执行轮次拆分的费用清单。 */
export function WorkitemCreditsBadge({ usage }: WorkitemCreditsBadgeProps) {
  const credits = usage?.credits ?? 0;
  if (!usage || !(credits > 0)) return null;

  return (
    <Popover content={<CreditsBreakdown usage={usage} />} trigger="hover">
      <Text
        data-testid="workitem-credits-badge"
        type="secondary"
        style={{ fontSize: 13, cursor: 'pointer', whiteSpace: 'nowrap' }}
      >
        <ThunderboltOutlined style={{ marginRight: 2 }} />
        {`${formatCreditsFixed(credits)} Credits`}
      </Text>
    </Popover>
  );
}
