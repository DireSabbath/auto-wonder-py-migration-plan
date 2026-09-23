export function formatTokenCount(n: number | null | undefined): string {
  if (n == null || n < 0) return '0';
  if (n < 1000) return String(n);
  if (n < 1_000_000) {
    const v = n / 1000;
    return (v % 1 === 0 ? v.toFixed(0) : v.toFixed(1).replace(/\.0$/, '')) + 'K';
  }
  const v = n / 1_000_000;
  return (v % 1 === 0 ? v.toFixed(0) : v.toFixed(1).replace(/\.0$/, '')) + 'M';
}

export function formatCredits(credits: number | null | undefined): string {
  if (credits == null || credits <= 0) return '0';
  if (credits < 0.01) return '<0.01';
  return credits.toFixed(2).replace(/\.00$/, '').replace(/(\.\d)0$/, '$1');
}

/** 固定两位小数，用于工单顶部的 credits 总计（`xx.xx Credits`）。 */
export function formatCreditsFixed(credits: number | null | undefined): string {
  if (credits == null || !Number.isFinite(credits) || credits <= 0) return '0.00';
  return credits.toFixed(2);
}

export function formatWithCommas(n: number): string {
  return n.toLocaleString('en-US');
}
