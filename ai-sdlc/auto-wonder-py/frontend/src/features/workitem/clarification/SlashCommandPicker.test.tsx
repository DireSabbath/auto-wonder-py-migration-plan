import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import {
  SlashCommandPicker,
  slashCommandQuery,
  filterSlashCommands,
} from './SlashCommandPicker';
import type { AcpSlashCommand } from './types';

const commands: AcpSlashCommand[] = [
  { name: 'quest', description: '生成需求问卷', input: { hint: '<主题>' } },
  { name: 'query', description: '查询上下文' },
  { name: 'commit', description: '提交改动', input: null },
];

describe('slashCommandQuery', () => {
  // F20：没有 / 前缀就不该进入补全态，否则正常打字会被劫持。
  it('only activates while the whole input is an unfinished slash token', () => {
    expect(slashCommandQuery('/')).toBe('');
    expect(slashCommandQuery('/qu')).toBe('qu');
    expect(slashCommandQuery('')).toBeNull();
    expect(slashCommandQuery('你好')).toBeNull();
    expect(slashCommandQuery('请用 /quest')).toBeNull();
    // 敲了空格说明命令已选定，后面是参数，不再补全
    expect(slashCommandQuery('/quest ')).toBeNull();
    expect(slashCommandQuery('/quest 登录')).toBeNull();
    expect(slashCommandQuery('/quest\n')).toBeNull();
  });
});

describe('filterSlashCommands', () => {
  it('matches by case-insensitive name prefix', () => {
    expect(filterSlashCommands(commands, 'qu').map((c) => c.name)).toEqual(['quest', 'query']);
    expect(filterSlashCommands(commands, 'QUE').map((c) => c.name)).toEqual(['quest', 'query']);
    expect(filterSlashCommands(commands, '').map((c) => c.name)).toEqual(['quest', 'query', 'commit']);
    expect(filterSlashCommands(commands, 'zzz')).toEqual([]);
  });
});

describe('SlashCommandPicker', () => {
  // F18：候选要把 name / description / hint 都摆出来，用户才知道该怎么填。
  it('lists matching commands with description and input hint', () => {
    render(<SlashCommandPicker commands={commands} query="qu" onSelect={vi.fn()} />);

    expect(screen.getByTestId('slash-command-picker')).toBeInTheDocument();
    expect(screen.getByText('/quest')).toBeInTheDocument();
    expect(screen.getByText('生成需求问卷')).toBeInTheDocument();
    expect(screen.getByText('<主题>')).toBeInTheDocument();
    expect(screen.getByText('/query')).toBeInTheDocument();
    expect(screen.queryByText('/commit')).toBeNull();
  });

  // F19：ACP 没有 invoke 方法，选中命令就是把 `/name ` 当普通文本填进输入框。
  it('hands the picked command back to the caller', () => {
    const onSelect = vi.fn();
    render(<SlashCommandPicker commands={commands} query="" onSelect={onSelect} />);

    fireEvent.click(screen.getByTestId('slash-command-quest'));

    expect(onSelect).toHaveBeenCalledWith(commands[0]);
  });

  it('can be picked with the keyboard as well as the mouse', () => {
    const onSelect = vi.fn();
    render(<SlashCommandPicker commands={commands} query="" onSelect={onSelect} />);

    fireEvent.keyDown(screen.getByTestId('slash-command-query'), { key: 'ArrowDown' });
    expect(onSelect).not.toHaveBeenCalled();

    fireEvent.keyDown(screen.getByTestId('slash-command-query'), { key: 'Enter' });
    expect(onSelect).toHaveBeenCalledWith(commands[1]);
  });

  it('omits the description line for a command that has none', () => {
    render(
      <SlashCommandPicker
        commands={[{ name: 'bare' }]}
        query=""
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByTestId('slash-command-bare').textContent).toBe('/bare');
  });

  // F20：执行器没吐 acp_commands 就没有候选，/ 不该弹出一个空浮层。
  it('renders nothing without commands, without a query, or without matches', () => {
    const empty = render(<SlashCommandPicker commands={[]} query="" onSelect={vi.fn()} />);
    expect(empty.container.textContent).toBe('');
    empty.unmount();

    const inactive = render(<SlashCommandPicker commands={commands} query={null} onSelect={vi.fn()} />);
    expect(inactive.container.textContent).toBe('');
    inactive.unmount();

    const noMatch = render(<SlashCommandPicker commands={commands} query="zzz" onSelect={vi.fn()} />);
    expect(noMatch.container.textContent).toBe('');
  });
});
