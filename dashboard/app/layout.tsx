import type { Metadata } from "next";
import { JetBrains_Mono, Space_Grotesk } from "next/font/google";
import "./globals.css";
import { Nav } from "@/components/Nav";
import { BurnChip } from "@/components/BurnChip";
import { McpChip } from "@/components/McpChip";
import { NotificationBell } from "@/components/NotificationBell";
import { TokenGate } from "@/components/TokenGate";
import {
  TerminalDockProvider,
  TerminalDockToggle,
} from "@/components/TerminalDock";

// The type pairing: Space Grotesk for UI (geometric, a little posh),
// JetBrains Mono for anything terminal-adjacent. Self-hosted at build time
// by next/font; wired into Tailwind via the variables in globals.css.
const grotesk = Space_Grotesk({
  subsets: ["latin"],
  variable: "--font-grotesk",
});
const mono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jbmono",
});

export const metadata: Metadata = {
  title: "Manifold",
  description: "Lambda Cloud GPU orchestrator",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={`${grotesk.variable} ${mono.variable}`}>
      <body className="min-h-screen bg-zinc-50 text-zinc-900 antialiased">
        {/* At the layout level so ANY page can recover from a 401: stores
            the Tauri handoff token and asks for a paste otherwise. */}
        <TokenGate />
        {/* The dock provider wraps everything so any page (instance cards
            included) can dock a shell, and sessions survive navigation. */}
        <TerminalDockProvider>
          <header className="sticky top-0 z-40 border-b border-zinc-200 bg-zinc-50/80 backdrop-blur">
            <div className="mx-auto flex max-w-6xl items-center gap-8 px-6 py-3">
              <span className="flex items-baseline gap-0.5 font-mono text-sm font-semibold tracking-tight">
                manifold
                <span className="cursor-blink text-teal-400">▌</span>
              </span>
              <Nav />
              <div className="ml-auto flex items-center gap-2">
                <McpChip />
                <BurnChip />
                <TerminalDockToggle />
                <NotificationBell />
              </div>
            </div>
          </header>
          <main className="mx-auto max-w-6xl px-6 py-8">{children}</main>
        </TerminalDockProvider>
      </body>
    </html>
  );
}
