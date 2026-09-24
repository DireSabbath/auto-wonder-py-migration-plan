import type { CSSProperties } from 'react';

/** 澄清面板的唯一色值与尺度来源。Codex 风格：白底 + 发丝线分层，不用灰底分层。 */
export const CLARIFICATION_THEME = {
  surface: '#ffffff',
  hairline: 'rgba(0,0,0,0.06)',
  controlBorder: 'rgba(0,0,0,0.10)',
  userBubble: '#f4f4f5',
  codeSurface: '#f7f7f8',
  codeBorder: 'rgba(0,0,0,0.05)',
  textPrimary: '#111827',
  textSecondary: '#6b7280',
  textMuted: '#9ca3af',
  radiusBubble: 14,
  radiusBlock: 8,
  radiusControl: 12,
} as const;

/** 用户消息气泡：浅灰底、无边框、大圆角，靠右。
 *  不设 whiteSpace——由调用方决定（纯文本要 pre-wrap，markdown 不能要）。 */
export function userBubbleStyle(): CSSProperties {
  return {
    padding: '10px 14px',
    borderRadius: CLARIFICATION_THEME.radiusBubble,
    backgroundColor: CLARIFICATION_THEME.userBubble,
    fontSize: 13,
    lineHeight: '1.7',
  };
}

/** AI 回复：平铺在白底上，无底色、无边框、无内距。
 *  流式态与完成态共用本函数，保证回复过程中与完成后渲染一致。 */
export function agentBlockStyle(): CSSProperties {
  return {
    fontSize: 13,
    lineHeight: '1.75',
    color: CLARIFICATION_THEME.textPrimary,
  };
}
