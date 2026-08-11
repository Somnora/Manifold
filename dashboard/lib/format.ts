export function formatBytes(n: number): string {
  if (n === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(Math.floor(Math.log2(n) / 10), units.length - 1);
  const value = n / 2 ** (10 * i);
  return `${value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${units[i]}`;
}

export function formatMoney(usd: number): string {
  return `$${usd.toFixed(2)}`;
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// There is deliberately no cost formula in this file. Spend is accounting,
// not formatting: the backend owns the single implementation (see
// /spend/summary and backend/app/spend.py), because a launch whose instance
// existed but whose end time was never observed has no point cost at all,
// and a client that substitutes "now" for the missing end grows that number
// forever. The dashboard renders what the backend answers.
