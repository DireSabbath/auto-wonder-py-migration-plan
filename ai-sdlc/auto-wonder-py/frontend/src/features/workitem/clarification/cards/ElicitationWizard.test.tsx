import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { ElicitationWizard } from './ElicitationWizard';

const schema = {
  type: 'object',
  properties: {
    q0: { type: 'string', title: '技术栈', oneOf: [{ const: 'A', title: 'A' }, { const: 'B', title: 'B' }] },
    q1: { type: 'array', title: '交付物', items: { anyOf: [{ const: 'C', title: 'C' }] } },
    q2: { type: 'string', title: '补充' },
  },
  required: ['q0', 'q1'],
};
const next = () => screen.getByTestId('elicitation-next-r1');
const prev = () => screen.getByTestId('elicitation-prev-r1');
const stepDot = (i: number) => screen.getByTestId(`elicitation-step-r1-${i}`);
const advance = () => act(() => { vi.advanceTimersByTime(320); });

describe('ElicitationWizard', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it('一次只显示一题，含计数与步骤点', () => {
    render(<ElicitationWizard requestId="r1" schema={schema} onReply={vi.fn()} />);
    expect(screen.getByTestId('elicitation-count-r1')).toHaveTextContent('第 1/3 题');
    expect(screen.getByTestId('elicitation-option-q0-0')).toBeInTheDocument();
    expect(screen.queryByTestId('elicitation-field-q2')).toBeNull();
    expect(stepDot(0)).toBeInTheDocument();
    expect(stepDot(2)).toBeInTheDocument();
  });

  it('把问题消息作为可见标题渲染', () => {
    render(<ElicitationWizard requestId="r1" message="请确认以下几点" schema={schema} onReply={vi.fn()} />);
    expect(screen.getByText('请确认以下几点')).toBeInTheDocument();
  });

  it('单选取值后自动进入下一题', () => {
    render(<ElicitationWizard requestId="r1" schema={schema} onReply={vi.fn()} />);
    fireEvent.click(screen.getByTestId('elicitation-option-q0-0'));
    advance();
    expect(screen.getByTestId('elicitation-count-r1')).toHaveTextContent('第 2/3 题');
  });

  it('多选题点「下一题」推进；上一题返回且答案保留', () => {
    render(<ElicitationWizard requestId="r1" schema={schema} onReply={vi.fn()} />);
    fireEvent.click(screen.getByTestId('elicitation-option-q0-0'));
    advance();
    fireEvent.click(screen.getByTestId('elicitation-option-q1-0'));
    fireEvent.click(next());
    expect(screen.getByTestId('elicitation-count-r1')).toHaveTextContent('第 3/3 题');
    fireEvent.click(prev());
    expect(screen.getByTestId('elicitation-count-r1')).toHaveTextContent('第 2/3 题');
    expect(screen.getByTestId('elicitation-option-q1-0')).toBeInTheDocument();
  });

  it('步骤点可跳回任意题', () => {
    render(<ElicitationWizard requestId="r1" schema={schema} onReply={vi.fn()} />);
    fireEvent.click(screen.getByTestId('elicitation-option-q0-0'));
    advance();
    fireEvent.click(stepDot(0));
    expect(screen.getByTestId('elicitation-count-r1')).toHaveTextContent('第 1/3 题');
  });

  it('必填未答点提交：跳到第一个缺题并标红，不回传', () => {
    const onReply = vi.fn();
    render(<ElicitationWizard requestId="r1" schema={schema} onReply={onReply} />);
    fireEvent.click(stepDot(2));
    fireEvent.click(screen.getByTestId('elicitation-submit-r1'));
    expect(onReply).not.toHaveBeenCalled();
    expect(screen.getByTestId('elicitation-count-r1')).toHaveTextContent('第 1/3 题');
    expect(screen.getByTestId('elicitation-error-q0')).toBeInTheDocument();
  });

  it('末题全答完提交：回传编码内容', () => {
    const onReply = vi.fn();
    render(<ElicitationWizard requestId="r1" schema={schema} onReply={onReply} />);
    fireEvent.click(screen.getByTestId('elicitation-option-q0-1'));
    advance();
    fireEvent.click(screen.getByTestId('elicitation-option-q1-0'));
    fireEvent.click(next());
    fireEvent.change(screen.getByTestId('elicitation-field-q2'), { target: { value: '备注' } });
    fireEvent.click(screen.getByTestId('elicitation-submit-r1'));
    expect(onReply).toHaveBeenCalledWith({ action: 'accept', content: { q0: 'B', q1: ['C'], q2: '备注' } });
  });

  it('跳过回传 decline', () => {
    const onReply = vi.fn();
    render(<ElicitationWizard requestId="r1" schema={schema} onReply={onReply} />);
    fireEvent.click(screen.getByTestId('elicitation-skip-r1'));
    expect(onReply).toHaveBeenCalledWith({ action: 'decline' });
  });

  it('单选选「有其他想法」填字后提交，文本替换原值', () => {
    const onReply = vi.fn();
    render(<ElicitationWizard requestId="r1" schema={schema} onReply={onReply} />);
    fireEvent.click(screen.getByTestId('elicitation-option-q0-other'));
    fireEvent.change(screen.getByTestId('elicitation-other-input-q0'), { target: { value: '自定义' } });
    fireEvent.click(next());
    fireEvent.click(screen.getByTestId('elicitation-option-q1-0'));
    fireEvent.click(next());
    fireEvent.click(screen.getByTestId('elicitation-submit-r1'));
    expect(onReply).toHaveBeenCalledWith({ action: 'accept', content: { q0: '自定义', q1: ['C'] } });
  });

  it('选「有其他想法」但空值，必填拦下并提示', () => {
    const onReply = vi.fn();
    render(<ElicitationWizard requestId="r1" schema={schema} onReply={onReply} />);
    fireEvent.click(screen.getByTestId('elicitation-option-q0-other'));
    fireEvent.click(next());
    expect(onReply).not.toHaveBeenCalled();
    expect(screen.getByTestId('elicitation-error-q0')).toHaveTextContent('请填写其他想法');
  });

  it('自动切换延时内手动导航，则不再叠加自动跳题', () => {
    render(<ElicitationWizard requestId="r1" schema={schema} onReply={vi.fn()} />);
    fireEvent.click(screen.getByTestId('elicitation-option-q0-0')); // 安排 300ms 后自动切到第2题
    fireEvent.click(stepDot(1));                                    // 延时内手动跳到第2题
    advance();                                                      // 旧定时器若仍生效会再叠加跳到第3题
    expect(screen.getByTestId('elicitation-count-r1')).toHaveTextContent('第 2/3 题');
  });

  it('多选勾选「有其他想法」但为空，必填拦下', () => {
    const onReply = vi.fn();
    render(<ElicitationWizard requestId="r1" schema={schema} onReply={onReply} />);
    fireEvent.click(screen.getByTestId('elicitation-option-q0-0'));
    advance();
    fireEvent.click(screen.getByTestId('elicitation-option-q1-other'));
    fireEvent.click(next());
    expect(onReply).not.toHaveBeenCalled();
    expect(screen.getByTestId('elicitation-error-q1')).toHaveTextContent('请填写其他想法');
  });

  it('多选勾选「有其他想法」并填写，提交时追加到数组末尾', () => {
    const onReply = vi.fn();
    render(<ElicitationWizard requestId="r1" schema={schema} onReply={onReply} />);
    fireEvent.click(screen.getByTestId('elicitation-option-q0-0'));
    advance();
    fireEvent.click(screen.getByTestId('elicitation-option-q1-0'));
    fireEvent.click(screen.getByTestId('elicitation-option-q1-other'));
    fireEvent.change(screen.getByTestId('elicitation-other-input-q1'), { target: { value: '要离线' } });
    fireEvent.click(next());
    fireEvent.click(screen.getByTestId('elicitation-submit-r1'));
    expect(onReply).toHaveBeenCalledWith({
      action: 'accept',
      content: { q0: 'A', q1: ['C', '补充想法：要离线'] },
    });
  });

  it('单选题作为末题时停在提交、不自动切换或提交', () => {
    const onReply = vi.fn();
    const singleLast = {
      type: 'object',
      properties: { q0: { type: 'string', title: '唯一题', oneOf: [{ const: 'A', title: 'A' }] } },
      required: ['q0'],
    };
    render(<ElicitationWizard requestId="r1" schema={singleLast} onReply={onReply} />);
    fireEvent.click(screen.getByTestId('elicitation-option-q0-0'));
    advance();
    expect(screen.getByTestId('elicitation-submit-r1')).toBeInTheDocument();
    expect(onReply).not.toHaveBeenCalled();
  });
});
