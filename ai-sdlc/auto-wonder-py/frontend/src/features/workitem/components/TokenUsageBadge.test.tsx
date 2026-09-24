import { describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { TokenUsageBadge, StepTokenBadge } from './TokenUsageBadge';
import type { UsageSummary } from '@/shared/types/workitem';

describe('TokenUsageBadge', () => {
  const usage: UsageSummary = {
    model: 'auto',
    inputTokens: 700_000,
    outputTokens: 300_000,
    cacheReadTokens: 100_000,
    reasoningTokens: 12_000,
    credits: 82.61,
  };

  it('renders credits next to the bolt icon and never renders token counts', () => {
    render(<TokenUsageBadge usage={usage} />);
    expect(screen.getByText('82.61 credits')).toBeInTheDocument();
    expect(screen.queryByText('1M')).not.toBeInTheDocument();
    expect(screen.queryByText(/tokens/i)).not.toBeInTheDocument();
  });

  it('shows only model and credits on hover, without any token row', async () => {
    render(<TokenUsageBadge usage={usage} />);
    fireEvent.mouseEnter(screen.getByText('82.61 credits'));
    expect(await screen.findByText(/Credits:/)).toBeInTheDocument();
    expect(screen.getByText('82.61')).toBeInTheDocument();
    expect(screen.getByText(/模型:/)).toBeInTheDocument();
    expect(screen.queryByText(/Total tokens:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Input tokens:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Output tokens:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Cached tokens:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Reasoning tokens:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Cache hit rate:/)).not.toBeInTheDocument();
    expect(screen.queryByText('700,000')).not.toBeInTheDocument();
    expect(screen.queryByText('300,000')).not.toBeInTheDocument();
  });

  it('hides the model row when showModel is false but keeps credits', async () => {
    render(<TokenUsageBadge usage={usage} showModel={false} />);
    fireEvent.mouseEnter(screen.getByText('82.61 credits'));
    expect(await screen.findByText(/Credits:/)).toBeInTheDocument();
    expect(screen.queryByText(/模型:/)).not.toBeInTheDocument();
    expect(screen.queryByText('auto')).not.toBeInTheDocument();
  });

  it('hides the model row when the usage carries no model', async () => {
    render(<TokenUsageBadge usage={{ credits: 1.5 }} />);
    fireEvent.mouseEnter(screen.getByText('1.5 credits'));
    expect(await screen.findByText(/Credits:/)).toBeInTheDocument();
    expect(screen.queryByText(/模型:/)).not.toBeInTheDocument();
  });

  it('renders a readable label for sub-cent credits', () => {
    render(<TokenUsageBadge usage={{ inputTokens: 900, outputTokens: 100, credits: 0.004 }} />);
    expect(screen.getByText('<0.01 credits')).toBeInTheDocument();
  });

  it('renders nothing when credits are missing even if tokens exist', () => {
    const { container } = render(<TokenUsageBadge usage={{ inputTokens: 700_000, outputTokens: 300_000 }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when credits are zero', () => {
    const { container } = render(<TokenUsageBadge usage={{ credits: 0 }} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe('StepTokenBadge', () => {
  const usage: UsageSummary = { inputTokens: 30_000, outputTokens: 13_000, credits: 4.3 };

  it('shows credits instead of the compact token count', () => {
    render(<StepTokenBadge usage={usage} />);
    expect(screen.getByText('4.3 credits')).toBeInTheDocument();
    expect(screen.queryByText('43K')).not.toBeInTheDocument();
    expect(screen.queryByText(/tokens/i)).not.toBeInTheDocument();
  });

  it('shows readable light credits text inside the dark hover tooltip', async () => {
    render(<StepTokenBadge usage={usage} />);
    fireEvent.mouseEnter(screen.getByText('4.3 credits'));
    const label = await screen.findByText(/Credits:/);
    expect(label).toHaveStyle({ color: 'rgba(255, 255, 255, 0.85)' });
    expect(screen.getByText('4.3')).toBeInTheDocument();
    expect(screen.queryByText(/Input tokens:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/模型:/)).not.toBeInTheDocument();
  });

  it('renders nothing when credits are missing even if tokens exist', () => {
    const { container } = render(<StepTokenBadge usage={{ inputTokens: 30_000, outputTokens: 13_000 }} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when credits are zero', () => {
    const { container } = render(<StepTokenBadge usage={{ credits: 0 }} />);
    expect(container).toBeEmptyDOMElement();
  });
});
