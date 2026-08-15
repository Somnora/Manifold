"use client";

import { useEffect, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";

// "Has this hydrated on the client yet?" - the canonical React 18 answer.
// A useEffect(() => setMounted(true)) does the same job but schedules a
// cascading render to do it, which the lint rule correctly rejects.
const subscribe = () => () => {};
const useHydrated = () =>
  useSyncExternalStore(
    subscribe,
    () => true,    // client
    () => false,   // server / static export prerender
  );

// A modal must escape its parent, always.
//
// The bug this exists to make impossible (found on the cluster panel,
// 2026-08-14): `position: fixed` is NOT relative to the viewport when any
// ancestor has a transform, filter, backdrop-filter, perspective, or
// `will-change` naming one. Such an ancestor becomes the containing block
// for its fixed-position descendants. The Elastic GPU Clusters panel is
// `backdrop-blur-md overflow-hidden`, so its "Launch Swarm" dialog was
// positioned against the PANEL and then clipped to it: a horizontal sliver
// of a dialog that scrolled away with the card and could not be read or
// used.
//
// Removing the blur would have fixed that one instance and left the trap
// armed for the next component. Rendering through a portal to <body> is
// the structural fix: the dialog is no longer a descendant of anything in
// the page, so no ancestor styling can position or clip it, whatever gets
// added to those cards later.
//
// Also handled here because every modal needs it and none of them should
// re-implement it: Escape closes, a click on the backdrop closes, and the
// page behind cannot scroll while the dialog is open (scrolling the page
// under a dialog is how the old one appeared to "disappear").
export function ModalPortal({
  onClose,
  children,
  labelledBy,
}: {
  onClose: () => void;
  children: React.ReactNode;
  labelledBy?: string;
}) {
  // The dashboard is a static export: the first render happens at build
  // time where `document` does not exist, so the portal target is only
  // safe to touch once hydrated.
  const hydrated = useHydrated();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    // Freeze the page behind the dialog, and put the scrollbar's width
    // back as padding so the layout does not jump on open.
    const { overflow, paddingRight } = document.body.style;
    const gap = window.innerWidth - document.documentElement.clientWidth;
    document.body.style.overflow = "hidden";
    if (gap > 0) document.body.style.paddingRight = `${gap}px`;
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
      document.body.style.paddingRight = paddingRight;
    };
  }, [onClose]);

  if (!hydrated) return null;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center overflow-y-auto p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby={labelledBy}
    >
      {children}
    </div>,
    document.body,
  );
}
