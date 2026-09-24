import { describe, expect, it } from 'vitest';
import {
  CLARIFY_AGENT_PARAM,
  CLARIFY_CONVERSATION_PARAM,
  CLARIFY_FULLSCREEN_PARAM,
  CLARIFY_FULLSCREEN_VALUE,
  CLARIFY_PANEL_PARAM,
  CLARIFY_PANEL_VALUE,
  isSameClarifyView,
  readClarifyView,
  writeClarifyView,
} from './clarifyView';

/** 澄清视图挂在 URL 上，刷新才能停在原页面（工单 53035）。
 *  这是纯编解码层，页面/面板的所有恢复行为都由它兜底，所以按分支逐条钉死。 */
describe('readClarifyView', () => {
  it('reads the progress view when the panel param is absent', () => {
    expect(readClarifyView(new URLSearchParams(''))).toEqual({
      mode: 'progress',
      fullscreen: false,
      agentId: null,
      conversationId: null,
    });
  });

  it('ignores stray fullscreen/agent params when the panel param is absent', () => {
    // 只带 fullscreen=1 而不带 panel=clarify 的链接（例如手工拼错）不该把页面拽进澄清
    const view = readClarifyView(
      new URLSearchParams(`${CLARIFY_FULLSCREEN_PARAM}=${CLARIFY_FULLSCREEN_VALUE}&${CLARIFY_AGENT_PARAM}=42`),
    );
    expect(view.mode).toBe('progress');
    expect(view.fullscreen).toBe(false);
    expect(view.agentId).toBeNull();
  });

  it('reads a non-fullscreen clarify view', () => {
    const view = readClarifyView(new URLSearchParams(`${CLARIFY_PANEL_PARAM}=${CLARIFY_PANEL_VALUE}`));
    expect(view).toEqual({
      mode: 'clarify',
      fullscreen: false,
      agentId: null,
      conversationId: null,
    });
  });

  it('reads fullscreen only from the exact flag value', () => {
    expect(readClarifyView(new URLSearchParams('panel=clarify&fullscreen=1')).fullscreen).toBe(true);
    expect(readClarifyView(new URLSearchParams('panel=clarify&fullscreen=0')).fullscreen).toBe(false);
    expect(readClarifyView(new URLSearchParams('panel=clarify&fullscreen=true')).fullscreen).toBe(false);
    expect(readClarifyView(new URLSearchParams('panel=clarify&fullscreen=')).fullscreen).toBe(false);
  });

  it('reads positive integer agent and conversation ids', () => {
    const view = readClarifyView(new URLSearchParams('panel=clarify&agent=7&conversation=101'));
    expect(view.agentId).toBe(7);
    expect(view.conversationId).toBe(101);
  });

  it.each([
    ['空值', ''],
    ['非数字', 'abc'],
    ['负数', '-3'],
    ['小数', '1.5'],
    ['零', '0'],
    ['超出安全整数', '99999999999999999999'],
  ])('treats an invalid id (%s) as absent', (_label, raw) => {
    const view = readClarifyView(new URLSearchParams(`panel=clarify&agent=${raw}&conversation=${raw}`));
    expect(view.agentId).toBeNull();
    expect(view.conversationId).toBeNull();
  });

  it('does not treat an unrelated panel value as clarify', () => {
    expect(readClarifyView(new URLSearchParams('panel=progress')).mode).toBe('progress');
  });
});

describe('writeClarifyView', () => {
  it('adds the panel param when switching into clarify', () => {
    const next = writeClarifyView(new URLSearchParams(''), { mode: 'clarify' });
    expect(next.get(CLARIFY_PANEL_PARAM)).toBe(CLARIFY_PANEL_VALUE);
    expect(next.has(CLARIFY_FULLSCREEN_PARAM)).toBe(false);
  });

  it('keeps the current mode when the patch omits it', () => {
    const current = new URLSearchParams('panel=clarify&fullscreen=1');
    const next = writeClarifyView(current, { agentId: 7 });
    expect(next.get(CLARIFY_PANEL_PARAM)).toBe(CLARIFY_PANEL_VALUE);
    expect(next.get(CLARIFY_FULLSCREEN_PARAM)).toBe(CLARIFY_FULLSCREEN_VALUE);
    expect(next.get(CLARIFY_AGENT_PARAM)).toBe('7');
  });

  it('sets and clears the fullscreen flag', () => {
    const on = writeClarifyView(new URLSearchParams('panel=clarify'), { fullscreen: true });
    expect(on.get(CLARIFY_FULLSCREEN_PARAM)).toBe(CLARIFY_FULLSCREEN_VALUE);

    const off = writeClarifyView(on, { fullscreen: false });
    expect(off.has(CLARIFY_FULLSCREEN_PARAM)).toBe(false);
    // 退出全屏不能顺手把面板也关掉
    expect(off.get(CLARIFY_PANEL_PARAM)).toBe(CLARIFY_PANEL_VALUE);
  });

  it('clears fullscreen, agent and conversation when returning to progress', () => {
    const current = new URLSearchParams('panel=clarify&fullscreen=1&agent=7&conversation=101');
    const next = writeClarifyView(current, { mode: 'progress' });
    expect(next.toString()).toBe('');
  });

  it('clears fullscreen even when the patch asks for progress plus fullscreen', () => {
    // 非澄清态下 fullscreen 没有意义，留着会让下一次进入澄清莫名全屏
    const next = writeClarifyView(new URLSearchParams('panel=clarify'), {
      mode: 'progress',
      fullscreen: true,
    });
    expect(next.has(CLARIFY_FULLSCREEN_PARAM)).toBe(false);
    expect(next.has(CLARIFY_PANEL_PARAM)).toBe(false);
  });

  it('distinguishes an omitted id from an explicitly cleared one', () => {
    const current = new URLSearchParams('panel=clarify&agent=7&conversation=101');
    expect(writeClarifyView(current, {}).get(CLARIFY_AGENT_PARAM)).toBe('7');
    expect(writeClarifyView(current, { agentId: null }).has(CLARIFY_AGENT_PARAM)).toBe(false);
    expect(writeClarifyView(current, { agentId: null }).get(CLARIFY_CONVERSATION_PARAM)).toBe('101');
    expect(writeClarifyView(current, { conversationId: null }).has(CLARIFY_CONVERSATION_PARAM)).toBe(false);
  });

  it('preserves unrelated query params', () => {
    // AppLayout 的 workspaceId 深链清理依赖这些参数还在
    const current = new URLSearchParams('workspaceId=10002&tab=timeline');
    const next = writeClarifyView(current, { mode: 'clarify', fullscreen: true });
    expect(next.get('workspaceId')).toBe('10002');
    expect(next.get('tab')).toBe('timeline');

    const back = writeClarifyView(next, { mode: 'progress' });
    expect(back.get('workspaceId')).toBe('10002');
    expect(back.get('tab')).toBe('timeline');
    expect(back.has(CLARIFY_PANEL_PARAM)).toBe(false);
  });

  it('does not mutate the incoming params', () => {
    const current = new URLSearchParams('panel=clarify&agent=7');
    writeClarifyView(current, { mode: 'progress', conversationId: 3 });
    expect(current.toString()).toBe('panel=clarify&agent=7');
  });

  it('keeps a semantically identical view unchanged even when the key order differs', () => {
    // URLSearchParams.toString() 对键顺序敏感（delete 后再 set 会把键挪到末尾），
    // 页面靠语义去重决定要不要导航，所以不变量落在字段比较上而不是字符串比较上。
    const reordered = new URLSearchParams('conversation=101&agent=7&fullscreen=1&panel=clarify');
    const canonical = new URLSearchParams('panel=clarify&fullscreen=1&agent=7&conversation=101');
    expect(reordered.toString()).not.toBe(canonical.toString());

    const next = writeClarifyView(reordered, {
      mode: 'clarify',
      fullscreen: true,
      agentId: 7,
      conversationId: 101,
    });
    expect(isSameClarifyView(readClarifyView(next), readClarifyView(canonical))).toBe(true);
  });

  it('round-trips a full clarify view through read and write', () => {
    const written = writeClarifyView(new URLSearchParams(''), {
      mode: 'clarify',
      fullscreen: true,
      agentId: 42,
      conversationId: 101,
    });
    expect(readClarifyView(written)).toEqual({
      mode: 'clarify',
      fullscreen: true,
      agentId: 42,
      conversationId: 101,
    });
  });
});

/** 页面写回 URL 前靠它决定要不要导航（CR 反馈 NB-2）。
 *  四个字段逐一钉死：漏比任何一个都会让 URL 与视图状态脱节。 */
describe('isSameClarifyView', () => {
  const base = {
    mode: 'clarify' as const,
    fullscreen: true,
    agentId: 42,
    conversationId: 101,
  };

  it('treats an identical view as unchanged', () => {
    expect(isSameClarifyView(base, { ...base })).toBe(true);
  });

  it('detects a mode change', () => {
    expect(isSameClarifyView(base, { ...base, mode: 'progress' })).toBe(false);
  });

  it('detects a fullscreen change', () => {
    expect(isSameClarifyView(base, { ...base, fullscreen: false })).toBe(false);
  });

  it('detects an agent change, including clearing it', () => {
    expect(isSameClarifyView(base, { ...base, agentId: 7 })).toBe(false);
    expect(isSameClarifyView(base, { ...base, agentId: null })).toBe(false);
  });

  it('detects a conversation change, including clearing it', () => {
    expect(isSameClarifyView(base, { ...base, conversationId: 102 })).toBe(false);
    expect(isSameClarifyView(base, { ...base, conversationId: null })).toBe(false);
  });

  it('treats stray params on a non-clarify URL as the same progress view', () => {
    // 读取阶段已把非澄清态的残留参数归零，写回时就不必为它多导航一次
    expect(isSameClarifyView(
      readClarifyView(new URLSearchParams('fullscreen=1&agent=42&conversation=101')),
      readClarifyView(new URLSearchParams('')),
    )).toBe(true);
  });
});
