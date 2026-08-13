const tones = {
  green: "bg-emerald-100 text-emerald-800",
  amber: "bg-amber-100 text-amber-800",
  red: "bg-red-100 text-red-800",
  zinc: "bg-zinc-100 text-zinc-600",
} as const;

export type Tone = keyof typeof tones;

// One badge component for instance status, connection state, and launch
// status. `pulse` marks in-flight states so the eye finds them.
export function Badge({
  label,
  tone,
  pulse = false,
}: {
  label: string;
  tone: Tone;
  pulse?: boolean;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ${tones[tone]}`}
    >
      {pulse && (
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
      )}
      {label}
    </span>
  );
}

export function statusTone(status: string): { tone: Tone; pulse: boolean } {
  switch (status) {
    case "active":
    case "connected":
    case "succeeded":
    case "available":
    case "launched":
      return { tone: "green", pulse: false };
    case "booting":
    case "launching":
    case "retrying":
    case "connecting":
    case "reconnecting":
    case "queued":
    case "running":
    case "watching":
      return { tone: "amber", pulse: true };
    case "failed":
    case "unhealthy":
    case "preempted":
      return { tone: "red", pulse: false };
    case "skipped":
      // Deliberately NOT red: a skipped job never ran (its dependency did
      // not succeed). The failure is the parent's; red here would double-
      // report one root cause down the whole chain.
      return { tone: "zinc", pulse: false };
    default:
      return { tone: "zinc", pulse: false };
  }
}

export function StatusBadge({ status }: { status: string }) {
  const { tone, pulse } = statusTone(status);
  return <Badge label={status} tone={tone} pulse={pulse} />;
}
