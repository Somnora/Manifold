"use client";

// The one honest sentence when a page's polling fails.
//
// Every page that polls has an empty state, and `(data ?? []).length === 0`
// makes "the backend did not answer" and "there is genuinely nothing here"
// render identically. During the very freeze this dashboard exists to
// survive, Jobs said "No active jobs." and Autopilot said "No runs yet." -
// positive claims manufactured from a request that never returned - while
// instances billed. This banner is the alternative: the same treatment the
// home page already gives its instance list (app/page.tsx), shared so the
// other pages cannot drift back into asserting what they do not know.
export function PollErrorBanner({
  error,
  stale,
  lastSuccess,
  what,
}: {
  error: string;
  stale: boolean;
  lastSuccess: Date | null;
  what: string;
}) {
  if (!error) return null;
  return (
    <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
      {error}
      {stale && lastSuccess ? (
        <span className="mt-1 block font-medium">
          The {what} below is a snapshot from{" "}
          {lastSuccess.toLocaleTimeString()} — NOT live. Things may have
          changed since; nothing here is interactive until the backend
          answers again.
        </span>
      ) : (
        <span className="mt-1 block font-medium">
          The {what} cannot be shown yet: nothing has loaded, and an empty
          list here would be a guess, not a fact.
        </span>
      )}
    </p>
  );
}
