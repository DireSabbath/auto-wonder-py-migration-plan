/**
 * 需求澄清视图状态的 URL 编解码。
 *
 * 澄清视图（进度/澄清、全屏、当前数字人、当前会话）此前只活在 React 内存态里，
 * 浏览器一刷新就全部丢失，用户被弹回工单进度上下文。把这几个值挂到
 * `/workitems/:id` 的查询参数上之后，刷新与分享链接都能落回同一屏。
 *
 * 这里是纯函数、不碰 router，方便把畸形参数（负数、非数字、超范围）挡在组件外。
 */

export type ClarifyPanelMode = 'progress' | 'clarify';

/** 数字人与会话是澄清面板内部的上下文，恢复时成对下发。 */
export interface ClarifyContext {
  agentId: number | null;
  conversationId: number | null;
}

export interface ClarifyView extends ClarifyContext {
  mode: ClarifyPanelMode;
  fullscreen: boolean;
}

export const CLARIFY_PANEL_PARAM = 'panel';
export const CLARIFY_PANEL_VALUE = 'clarify';
export const CLARIFY_FULLSCREEN_PARAM = 'fullscreen';
export const CLARIFY_FULLSCREEN_VALUE = '1';
export const CLARIFY_AGENT_PARAM = 'agent';
export const CLARIFY_CONVERSATION_PARAM = 'conversation';

const PROGRESS_VIEW: ClarifyView = {
  mode: 'progress',
  fullscreen: false,
  agentId: null,
  conversationId: null,
};

function readPositiveInt(raw: string | null): number | null {
  if (!raw || !/^\d+$/.test(raw)) return null;
  const value = Number(raw);
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

export function readClarifyView(search: URLSearchParams): ClarifyView {
  // 非澄清态下全屏/数字人/会话都没有意义，直接归零：
  // 手改 URL 或旧链接残留参数都不该让页面进入自相矛盾的状态。
  if (search.get(CLARIFY_PANEL_PARAM) !== CLARIFY_PANEL_VALUE) {
    return { ...PROGRESS_VIEW };
  }
  return {
    mode: 'clarify',
    fullscreen: search.get(CLARIFY_FULLSCREEN_PARAM) === CLARIFY_FULLSCREEN_VALUE,
    agentId: readPositiveInt(search.get(CLARIFY_AGENT_PARAM)),
    conversationId: readPositiveInt(search.get(CLARIFY_CONVERSATION_PARAM)),
  };
}

/** URLSearchParams.toString() 对键顺序敏感（delete 后再 set 会把键挪到末尾），
 *  用它去重会把语义等价、只是换了顺序的写入当成变化，多触发一次 replace 导航。
 *  写回前按澄清视图的四个字段逐项比较才是语义去重。 */
export function isSameClarifyView(left: ClarifyView, right: ClarifyView): boolean {
  return (
    left.mode === right.mode
    && left.fullscreen === right.fullscreen
    && left.agentId === right.agentId
    && left.conversationId === right.conversationId
  );
}

export function writeClarifyView(
  search: URLSearchParams,
  patch: Partial<ClarifyView>,
): URLSearchParams {
  const next = new URLSearchParams(search);
  const current = readClarifyView(next);
  const merged: ClarifyView = {
    mode: patch.mode ?? current.mode,
    fullscreen: patch.fullscreen ?? current.fullscreen,
    agentId: patch.agentId !== undefined ? patch.agentId : current.agentId,
    conversationId: patch.conversationId !== undefined
      ? patch.conversationId
      : current.conversationId,
  };
  // 返回进度就没有「澄清视图」可言：全屏与会话上下文一并清掉，
  // 否则下一次进入澄清会被上一轮的旧参数绑架。
  if (merged.mode === 'progress') {
    merged.fullscreen = false;
    merged.agentId = null;
    merged.conversationId = null;
  }

  setOrDelete(next, CLARIFY_PANEL_PARAM, merged.mode === 'clarify' ? CLARIFY_PANEL_VALUE : null);
  setOrDelete(next, CLARIFY_FULLSCREEN_PARAM, merged.fullscreen ? CLARIFY_FULLSCREEN_VALUE : null);
  setOrDelete(next, CLARIFY_AGENT_PARAM, merged.agentId == null ? null : String(merged.agentId));
  setOrDelete(
    next,
    CLARIFY_CONVERSATION_PARAM,
    merged.conversationId == null ? null : String(merged.conversationId),
  );
  return next;
}

function setOrDelete(params: URLSearchParams, key: string, value: string | null): void {
  if (value == null) {
    params.delete(key);
    return;
  }
  params.set(key, value);
}
