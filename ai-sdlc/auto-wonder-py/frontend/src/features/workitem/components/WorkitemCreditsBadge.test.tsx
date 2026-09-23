import { describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { WorkitemCreditsBadge } from './WorkitemCreditsBadge';
import type { WorkitemUsageSummary } from '@/shared/types/workitem';

const usage: WorkitemUsageSummary = {
  credits: 70,
  runs: [
    { agentId: 40, agentName: 'DEV', runIndex: 1, label: 'DEV run-1', credits: 20 },
    { agentId: 41, agentName: 'CR', runIndex: 1, label: 'CR run-1', credits: 20 },
    { agentId: 40, agentName: 'DEV', runIndex: 2, label: 'DEV run-2', credits: 30 },
  ],
};

async function openBreakdown() {
  fireEvent.mouseEnter(screen.getByTestId('workitem-credits-badge'));
  return screen.findByTestId('workitem-credits-breakdown');
}

describe('WorkitemCreditsBadge', () => {
  it('renders the accumulated credits with two decimals next to a bolt icon', () => {
    render(<WorkitemCreditsBadge usage={usage} />);

    const badge = screen.getByTestId('workitem-credits-badge');
    expect(badge).toHaveTextContent('70.00 Credits');
    expect(badge.querySelector('.anticon-thunderbolt')).not.toBeNull();
  });

  it('keeps trailing zeros for whole-number credits', () => {
    render(<WorkitemCreditsBadge usage={{ credits: 20, runs: [] }} />);
    expect(screen.getByTestId('workitem-credits-badge')).toHaveTextContent('20.00 Credits');
  });

  it('lists every run and the total on hover', async () => {
    render(<WorkitemCreditsBadge usage={usage} />);

    const breakdown = await openBreakdown();

    expect(breakdown.textContent).toContain('DEV run-1: 20 credits');
    expect(breakdown.textContent).toContain('CR run-1: 20 credits');
    expect(breakdown.textContent).toContain('DEV run-2: 30 credits');
    expect(breakdown.textContent).toContain('Total: 70.00 Credits');
  });

  it('renders nothing before hover', () => {
    render(<WorkitemCreditsBadge usage={usage} />);
    expect(screen.queryByTestId('workitem-credits-breakdown')).not.toBeInTheDocument();
  });

  it('builds the run label from agent name and run index when the backend omits it', async () => {
    render(
      <WorkitemCreditsBadge
        usage={{ credits: 12.5, runs: [{ agentId: 40, agentName: 'DEV', runIndex: 2, credits: 12.5 }] }}
      />,
    );

    const breakdown = await openBreakdown();

    expect(breakdown.textContent).toContain('DEV run-2: 12.5 credits');
    expect(breakdown.textContent).toContain('Total: 12.50 Credits');
  });

  it('falls back to the agent id, then to unknown, when no name is available', async () => {
    render(
      <WorkitemCreditsBadge
        usage={{
          credits: 5,
          runs: [
            { agentId: 77, runIndex: 1, credits: 3 },
            { credits: 2 },
          ],
        }}
      />,
    );

    const breakdown = await openBreakdown();

    expect(breakdown.textContent).toContain('agent-77 run-1: 3 credits');
    expect(breakdown.textContent).toContain('unknown run-2: 2 credits');
  });

  it('skips runs without positive credits but keeps the total', async () => {
    render(
      <WorkitemCreditsBadge
        usage={{
          credits: 8,
          runs: [
            { agentId: 40, agentName: 'DEV', runIndex: 1, label: 'DEV run-1', credits: 0 },
            { agentId: 40, agentName: 'DEV', runIndex: 2, label: 'DEV run-2', credits: null },
            { agentId: 40, agentName: 'DEV', runIndex: 3, label: 'DEV run-3', credits: 8 },
          ],
        }}
      />,
    );

    const breakdown = await openBreakdown();

    expect(breakdown.textContent).not.toContain('DEV run-1');
    expect(breakdown.textContent).not.toContain('DEV run-2');
    expect(breakdown.textContent).toContain('DEV run-3: 8 credits');
    expect(breakdown.textContent).toContain('Total: 8.00 Credits');
  });

  it('shows the total even when the run breakdown is missing', async () => {
    render(<WorkitemCreditsBadge usage={{ credits: 3.14159 }} />);

    const breakdown = await openBreakdown();

    expect(breakdown.textContent).toContain('Total: 3.14 Credits');
  });

  it('renders nothing when usage is missing, empty or zero', () => {
    expect(render(<WorkitemCreditsBadge />).container).toBeEmptyDOMElement();
    expect(render(<WorkitemCreditsBadge usage={null} />).container).toBeEmptyDOMElement();
    expect(render(<WorkitemCreditsBadge usage={{}} />).container).toBeEmptyDOMElement();
    expect(render(<WorkitemCreditsBadge usage={{ credits: 0, runs: usage.runs }} />).container)
      .toBeEmptyDOMElement();
    expect(render(<WorkitemCreditsBadge usage={{ credits: -5 }} />).container).toBeEmptyDOMElement();
  });
});
