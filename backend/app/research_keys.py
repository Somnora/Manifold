"""The research-key vault: third-party API keys, one audited place.

THE PROBLEM (owner's words, 2026-08-17): research keys - YouTube, X, the
congress.gov keys behind Tally's pipeline - live scattered across every
agent's own dotfolder. Each new agent CLI means re-plumbing every key, and
a rotated key means hunting the copies. Manifold is the one thing every
agent on this machine already connects to, so the consolidated bucket
lives behind it: any client that can reach the guarded backend can list,
deposit, and fetch keys, and every fetch is audited with a purpose.

TWO FILES, TWO BLAST RADII - the load-bearing design point. Manifold's own
credentials (.env: the Lambda key, S3 keys, the API token) are never
reachable through this store. The vault reads exactly one file,
research-keys.env, so the handout endpoint STRUCTURALLY cannot leak the
backend's own secrets: a request for "lambda_api_key" is a 404, not a
disclosure, no matter who asks or why.

Values live in the FILE, never in SQLite (preferences.py's "never
secrets" rule) and never in a log or an audit row. SQLite holds only
annotation: note, provenance, last use. The file is deliberately
hand-editable - a human with a text editor is a supported client, so
rewrites preserve their comments and line order.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

# Lowercase snake_case, like template parameter names. The name is an
# identifier agents pass around and templates may someday reference; a
# strict shape now beats escaping rules forever.
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

_HEADER = (
    "# Manifold research keys. One NAME=value per line.\n"
    "# Managed from Settings and the research-key MCP tools; hand edits\n"
    "# (including these comments) are honored and preserved.\n"
)


def validate_name(name: str) -> str | None:
    """The complaint, or None if the name is fine."""
    if not NAME_RE.match(name):
        return (
            f"research-key names are lowercase snake_case, 1-63 chars "
            f"(a-z, 0-9, _), starting with a letter; got {name!r}"
        )
    return None


def validate_value(value: str) -> str | None:
    """The complaint, or None if the value is storable.

    Rejections instead of silent repair: stripping whitespace would MUTATE
    a secret, and a mutated secret fails downstream with an error that
    points at the wrong party. The one thing we refuse to guess about is
    the exact bytes of a credential.
    """
    if not value:
        return "an empty value is not a key; use DELETE to remove one"
    if len(value) > 4096:
        return "value exceeds 4096 chars; that is not an API key"
    if value != value.strip():
        return (
            "value has leading or trailing whitespace - almost always a "
            "paste artifact that would cause mystery auth failures "
            "downstream. Trim it and set again."
        )
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return "value contains control characters (newline/tab?); not storable"
    return None


class ResearchKeyStore:
    """File-backed key store. Synchronous by design: every operation is a
    single read-modify-replace with no awaits inside, so the asyncio
    backend cannot interleave two writers mid-rewrite."""

    def __init__(self, path: Path):
        self.path = Path(path)

    # -- reading ------------------------------------------------------------

    def _lines(self) -> list[str]:
        try:
            return self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []

    def _entries(self) -> dict[str, str]:
        entries: dict[str, str] = {}
        for line in self._lines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, _, value = stripped.partition("=")
            entries[name.strip()] = value
        return entries

    def names(self) -> dict[str, int]:
        """name -> value length. Presence and length are the ONLY facts
        this ever exports in bulk; values leave one at a time via get()."""
        return {name: len(value) for name, value in self._entries().items()}

    def get(self, name: str) -> str | None:
        return self._entries().get(name)

    # -- writing ------------------------------------------------------------

    def _write(self, lines: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace; the temp file is 0600 from birth so the value
        # never exists on disk world-readable, not even for a moment.
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                   prefix=".research-keys-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + ("\n" if lines else ""))
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def set(self, name: str, value: str) -> bool:
        """Upsert. Returns True if a value was replaced, False if created.
        Rewrites only the one line; comments and neighbors stay verbatim."""
        lines = self._lines()
        if not lines:
            lines = _HEADER.splitlines()
        replaced = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            if stripped.partition("=")[0].strip() == name:
                lines[i] = f"{name}={value}"
                replaced = True
                break
        if not replaced:
            lines.append(f"{name}={value}")
        self._write(lines)
        return replaced

    def delete(self, name: str) -> bool:
        lines = self._lines()
        kept = [
            line for line in lines
            if not ("=" in line.strip()
                    and not line.strip().startswith("#")
                    and line.strip().partition("=")[0].strip() == name)
        ]
        if len(kept) == len(lines):
            return False
        self._write(kept)
        return True
