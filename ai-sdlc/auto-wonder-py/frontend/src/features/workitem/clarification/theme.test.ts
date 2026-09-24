import { describe, expect, it } from 'vitest';
import { CLARIFICATION_THEME, agentBlockStyle, userBubbleStyle } from './theme';

describe('clarification theme', () => {
  it('keeps agent replies flat: no background, border or padding', () => {
    // 方案 A 的核心不变量：AI 回复直接落在白底上，不套气泡。
    // 一旦有人给它加回底色或边框，这条会红。
    const style = agentBlockStyle();
    expect(style).not.toHaveProperty('backgroundColor');
    expect(style).not.toHaveProperty('border');
    expect(style).not.toHaveProperty('padding');
  });

  it('gives user bubbles a borderless tinted surface', () => {
    const style = userBubbleStyle();
    expect(style.backgroundColor).toBe(CLARIFICATION_THEME.userBubble);
    expect(style).not.toHaveProperty('border');
    expect(style.borderRadius).toBe(CLARIFICATION_THEME.radiusBubble);
  });

  it('never sets whiteSpace on either style', () => {
    // 继承 pre-wrap 会把 markdown 块级元素间的换行渲染成字面空行，
    // 产生大块行间距空白；whiteSpace 必须由调用方按内容类型决定。
    expect(userBubbleStyle()).not.toHaveProperty('whiteSpace');
    expect(agentBlockStyle()).not.toHaveProperty('whiteSpace');
  });

  it('exposes a white surface and hairline instead of grey fills', () => {
    // 这里刻意写字面值而不是引用 CLARIFICATION_THEME：这些色值与尺度是已定稿的视觉决策。
    // 若只拿常量和自己比，把 userBubble 退回旧的 antd 蓝 #e6f7ff、codeSurface 退回旧灰
    // #fafafa / #f6f8fa、radiusBubble 退回 8，测试依旧全绿——旧灰蓝配色就能悄悄回来。
    expect(CLARIFICATION_THEME.surface).toBe('#ffffff');
    expect(CLARIFICATION_THEME.hairline).toBe('rgba(0,0,0,0.06)');
    expect(CLARIFICATION_THEME.userBubble).toBe('#f4f4f5');
    expect(CLARIFICATION_THEME.codeSurface).toBe('#f7f7f8');
    expect(CLARIFICATION_THEME.codeBorder).toBe('rgba(0,0,0,0.05)');
    expect(CLARIFICATION_THEME.radiusBubble).toBe(14);
  });

  it('carries no fixed content width token', () => {
    // 全屏态正文列必须跟着视口铺满（工单 53035）。
    // 曾经的 contentMaxWidth: 760 是超宽屏横向空出大半空间的唯一来源，删掉后要钉住，
    // 否则下一次“限宽更好看”的改动会把它悄悄加回来。
    expect(CLARIFICATION_THEME).not.toHaveProperty('contentMaxWidth');
    expect(Object.keys(CLARIFICATION_THEME).some((key) => /width/i.test(key))).toBe(false);
  });
});
