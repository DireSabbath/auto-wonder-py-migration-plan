import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { WorkitemContent } from './WorkitemContent';
import { copyTextToClipboard } from '@/shared/lib/clipboard';

vi.mock('@/shared/lib/clipboard', () => ({
  copyTextToClipboard: vi.fn(),
}));

vi.mock('@/shared/auth/useAccessCommand', () => ({
  useAccessCommand: () => (_mode: string, _label: string, action: () => void) => action(),
}));

const copyMock = vi.mocked(copyTextToClipboard);

describe('WorkitemContent copy entry', () => {
  beforeEach(() => {
    copyMock.mockReset();
    copyMock.mockResolvedValue(true);
  });

  it('copies the workitem body markdown from the header entry', async () => {
    const md = '# 背景\n\n正文内容';
    render(<WorkitemContent title="示例工单" contentMd={md} />);

    await userEvent.click(screen.getByRole('button', { name: '复制内容' }));
    await userEvent.click(await screen.findByText('复制原始 Markdown'));

    expect(copyMock).toHaveBeenCalledTimes(1);
    expect(copyMock).toHaveBeenCalledWith(md);
  });

  it('copies readable plain text for the workitem body', async () => {
    render(<WorkitemContent title="示例工单" contentMd={'# 背景\n- 一项'} />);

    await userEvent.click(screen.getByRole('button', { name: '复制内容' }));
    await userEvent.click(await screen.findByText('复制纯文本'));

    expect(copyMock).toHaveBeenCalledWith('背景\n\n- 一项');
  });

  it('hides the copy entry when the body is empty', () => {
    render(<WorkitemContent title="示例工单" contentMd="   " />);

    expect(screen.queryByRole('button', { name: '复制内容' })).not.toBeInTheDocument();
  });
});

describe('WorkitemContent null body', () => {
  it('renders the section without crashing when contentMd is null', () => {
    render(<WorkitemContent title="空工单" contentMd={null} />);

    expect(screen.getByTestId('workitem-content-section')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '复制内容' })).not.toBeInTheDocument();
  });

  it('opens the editor with an empty body when contentMd is null', async () => {
    render(<WorkitemContent title="空工单" contentMd={null} />);

    await userEvent.click(screen.getByRole('button', { name: /编辑/ }));

    expect(screen.getByLabelText('工单正文')).toHaveValue('');
  });

  it('saves an empty-string body when editing a null-body workitem', async () => {
    const onSave = vi.fn();
    render(<WorkitemContent title="空工单" contentMd={null} onSave={onSave} />);

    await userEvent.click(screen.getByRole('button', { name: /编辑/ }));
    await userEvent.type(screen.getByLabelText('工单标题'), '补充标题');
    await userEvent.click(screen.getByRole('button', { name: /保存/ }));

    expect(onSave).toHaveBeenCalledWith({ title: '空工单补充标题', contentMd: '' });
  });
});

describe('WorkitemContent body overflow containment', () => {
  it('renders a long fenced code block in the body as a contained pre', () => {
    const md = [
      '工单详情页面中，工单正文部分情况会超出正文区域到右侧',
      '',
      '```',
      "MCP error -32602: Structured content does not match the tool's output schema: data/assigneeName must be string, data/assigneeDisplayName must be string",
      '```',
    ].join('\n');
    render(<WorkitemContent title="示例工单" contentMd={md} />);

    const section = screen.getByTestId('workitem-content-section');
    const pre = section.querySelector('pre');
    expect(pre).not.toBeNull();
    // jsdom 的 computed style 会省略默认值，直接断言内联声明
    expect(pre).toHaveStyle({ overflowX: 'auto', maxWidth: '100%' });
    expect(section).toHaveTextContent('工单详情页面中，工单正文部分情况会超出正文区域到右侧');
    expect(pre).toHaveTextContent('data/assigneeDisplayName must be string');
  });
});
