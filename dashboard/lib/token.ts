// Where the browser keeps the backend's API token (Phase 78).
//
// localStorage, keyed by backend origin so a dev dashboard on :3000 and
// the desktop app's same-origin export never overwrite each other's
// token. The token arrives one of two ways: the Tauri shell lands on
// /?token=<value> (stored and scrubbed by the TokenGate), or the user
// pastes it from .env when the gate asks. Never sent in an HTTP query
// string - only the Authorization header (requests) and ?token= on
// WebSockets, which cannot set headers.

import { API_BASE } from "./backend";

function storageKey(): string {
  const origin =
    API_BASE !== ""
      ? API_BASE
      : typeof window !== "undefined"
        ? window.location.origin
        : "";
  return `manifold-api-token:${origin}`;
}

export function getToken(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(storageKey()) ?? "";
  } catch {
    return ""; // storage blocked (private mode): the gate will re-ask
  }
}

export function setToken(token: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(storageKey(), token);
  } catch {
    // Storage blocked: requests this session still work if callers read
    // through getToken() after a reload prompts a re-paste.
  }
}

// Spread into fetch headers; empty when no token is stored (an open
// backend simply ignores the absence).
export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { authorization: `Bearer ${token}` } : {};
}

// 401 signal: request() fires this, the TokenGate listens. An event
// rather than shared state keeps lib code free of React.
export const UNAUTHORIZED_EVENT = "manifold:unauthorized";

export function notifyUnauthorized(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
}
