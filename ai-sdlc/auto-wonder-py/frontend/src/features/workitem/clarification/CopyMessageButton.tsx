import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Tooltip, message } from 'antd';
import { CheckOutlined, CopyOutlined } from '@ant-design/icons';
import { copyTextToClipboard } from '@/shared/lib/clipboard';

/** ✓ 反馈停留时长；与 spec D1 一致 */
export const COPY_FEEDBACK_MS = 1500;

interface CopyMessageButtonProps {
  text: string;
  /** 空闲态文案；已复制态固定为「已复制」 */
  label?: string;
}

/** 单击即复制一段文本，图标瞬变 ✓。刻意不弹 antd message：✓ 本身就是反馈，
 *  再弹一次是双重反馈（失败才需要 message，因为没有可视的失败态）。 */
export function CopyMessageButton({ text, label = '复制' }: CopyMessageButtonProps) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => () => {
    mountedRef.current = false;
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);

  const handleCopy = useCallback(async () => {
    const ok = await copyTextToClipboard(text);
    if (!ok) {
      message.error('复制失败，请检查浏览器剪贴板权限');
      return;
    }
    // await 期间可能已卸载（比如幽灵气泡被落库的 turn 顶掉），此时清理函数早就
    // 跑过了，再挂计时器就没人清得掉它——直接收手。
    if (!mountedRef.current) return;
    setCopied(true);
    // 连续点击时重置计时，而不是让旧计时提前把 ✓ 收回
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setCopied(false), COPY_FEEDBACK_MS);
  }, [text]);

  const name = copied ? '已复制' : label;

  return (
    <Tooltip title={name}>
      <Button
        type="text"
        size="small"
        icon={copied ? <CheckOutlined /> : <CopyOutlined />}
        aria-label={name}
        data-testid="copy-message-button"
        onClick={handleCopy}
      />
    </Tooltip>
  );
}
