"""The local model library: the model you distilled, on your own machine.

Phase 85. The distill chain ends with a scorecard and a merged student
sitting on a Lambda filesystem you pay to reach. This module is the last
mile - pull the quantized .gguf home, install it into Ollama, and it shows
up in Manifold's own brain picker (as `local:ollama/<name>`) with no new
brain code at all, because 127.0.0.1:11434 is already a probed endpoint.

This is the pure half: names, paths, the Modelfile text, and reading
`ollama list`. No subprocesses, no network, no clock - the route in main.py
owns those. Everything here is decidable without Ollama installed, which is
the point: the tests must run on a machine that has never heard of it.

PATH CONFINEMENT IS THE JOB. A pulled file is written by the backend to a
path derived from a name the caller chose, and the installed Modelfile
names that path to a program that will read it. So names are validated as
bare filenames and every destination is re-checked against the library root
after resolution, rather than trusted because it was built from a template.
"""

from __future__ import annotations

import re
from pathlib import Path

# The library lives under DATA_ROOT (beside manifold.db and .env), not in the
# repo and not in ~/Downloads: the backend has to be able to find a file again
# in order to install it, and a download the browser routed somewhere private
# is a file Manifold can only talk about.
LIBRARY_DIRNAME = "models"

# Only the format this seam exists to produce. A .safetensors directory is not
# something Ollama can run, and pulling one over SSH is the download this whole
# feature was designed to avoid.
GGUF_SUFFIX = ".gguf"

# A bare filename: no directories, no traversal, no leading dot.
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")

# Ollama model names. Lowercase by convention (Ollama folds case anyway) and
# no tag: the tag is Ollama's to add (`:latest`), and letting a caller pass one
# makes "is it already installed" a string-matching argument nobody wins.
_OLLAMA_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")

# Written into the Modelfile as-is, so anything that could start a new
# directive line is refused before it gets there.
_MODELFILE_UNSAFE = ("\n", "\r", '"')


class LocalModelError(Exception):
    """A name or path this module will not act on.

    The message is user-facing verbatim, so it names the offending value and
    what would have been acceptable instead.
    """


def library_dir(data_root: Path) -> Path:
    """Where pulled models live. Created by the caller, not here."""
    return Path(data_root) / LIBRARY_DIRNAME


def validate_gguf_name(name: str) -> str:
    """The filename a pulled model gets in the library.

    A bare filename ending in .gguf. Rejecting directories here is what
    makes `library_dir / name` safe to build at all - but the caller still
    re-checks containment afterwards (see `destination`), because one
    validator is a single point of failure and this one guards a write.
    """
    value = (name or "").strip()
    if not value:
        raise LocalModelError("name the .gguf file to pull")
    if not value.endswith(GGUF_SUFFIX):
        raise LocalModelError(
            f"'{value}' must end in {GGUF_SUFFIX}: this pulls the quantized "
            f"file that gguf-quantize wrote, not a model directory"
        )
    if not _FILENAME_RE.match(value):
        raise LocalModelError(
            f"'{value}' must be a bare filename of [A-Za-z0-9._-] with no "
            f"directories (the library path is built here, so a path is "
            f"never accepted from a caller)"
        )
    return value


def validate_ollama_name(name: str) -> str:
    """The name the model will answer to in Ollama, and in the brain picker.

    No tag: `ollama create foo` makes `foo:latest`, and accepting a tag here
    would mean two spellings of the same model in the picker.
    """
    value = (name or "").strip().lower()
    if not value:
        raise LocalModelError("name the model for Ollama, e.g. my-student")
    if ":" in value:
        raise LocalModelError(
            f"'{value}' must not carry a tag - Ollama adds :latest itself"
        )
    if not _OLLAMA_NAME_RE.match(value):
        raise LocalModelError(
            f"'{value}' must be 2-64 characters of lowercase [a-z0-9._-], "
            f"starting with a letter or digit"
        )
    return value


def default_ollama_name(gguf_name: str) -> str:
    """A suggested Ollama name from a .gguf filename.

    Only a suggestion: it goes in the field the user can edit. Slugged
    hard enough that the suggestion always passes validate_ollama_name.
    """
    stem = gguf_name[: -len(GGUF_SUFFIX)] if gguf_name.endswith(GGUF_SUFFIX) \
        else gguf_name
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-.")
    slug = slug[:64]
    return slug if len(slug) >= 2 else "student"


def destination(library: Path, name: str) -> Path:
    """Absolute path a pulled model is written to, confined to the library.

    Takes the library directory itself (not DATA_ROOT) so the caller holds
    exactly one path and a test can point it at a temp dir. The name is
    validated first and the result is re-checked against the root
    afterwards. Belt and braces on purpose: this returns the path the
    backend will open for writing.
    """
    root = Path(library).resolve()
    target = (root / validate_gguf_name(name)).resolve()
    if target.parent != root:
        raise LocalModelError(
            f"'{name}' resolves outside the model library ({root})"
        )
    return target


def partial_path(final: Path) -> Path:
    """Where a pull is written while it is still in flight.

    A transfer that dies halfway must not leave a plausible-looking .gguf
    in the library: the file is written here and renamed only once the
    last byte lands, so what appears in the library is always complete.
    """
    return final.with_name(final.name + ".partial")


def modelfile_text(gguf_path: Path) -> str:
    """The whole Modelfile: which file to run, and nothing else.

    No PARAMETER or TEMPLATE lines. A .gguf converted from a HuggingFace
    model carries its own chat template in its metadata, and a template
    guessed here would silently override a correct one - which reads to
    the user as "the distilled model babbles" rather than "Manifold added
    a wrong prompt format".
    """
    path = str(gguf_path)
    if any(bad in path for bad in _MODELFILE_UNSAFE):
        raise LocalModelError(
            f"refusing to write a Modelfile for a path containing a quote or "
            f"newline: {path!r}"
        )
    return f'FROM "{path}"\n'


def install_argv(executable: str, name: str, modelfile: Path) -> list[str]:
    """argv for `ollama create`. A list, never a shell string.

    Returned rather than run so the exact command is assertable in a test
    with no Ollama on the machine.
    """
    return [executable, "create", name, "-f", str(modelfile)]


def parse_ollama_list(stdout: str) -> list[str]:
    """Installed model names from `ollama list`, tags stripped.

    Tolerant by design: a table Ollama reformats in a later version should
    degrade to a slightly wrong "already installed" hint, never a crash on
    the way to a model the user can see with their own eyes.
    """
    names = []
    for line in (stdout or "").splitlines():
        token = line.strip().split()
        if not token:
            continue
        first = token[0]
        if first.upper() == "NAME" or "/" in first[:1]:
            continue           # header row
        names.append(first.split(":", 1)[0])
    return names


def is_installed(name: str, installed: list[str]) -> bool:
    """Whether `ollama create <name>` would overwrite something."""
    return name.strip().lower() in {n.strip().lower() for n in installed}
