# Claude for Open Source: Manifold application draft

Repo: https://github.com/Somnora/Manifold
Facts as of 2026-07-19: MIT licensed, v0.1.0 tagged, 168 commits, 10 days of
development, 480 tests, ~13k LOC (Python 75%, TypeScript 23%), 10 bundled job templates,
20 MCP tools, .dmg out with 20 early testers.

---

## Q1. Tell us about the project's reach and impact

Manifold is early and moving fast: MIT licensed, v0.1.0 tagged, 168 commits and 480
tests in its first ten days. The desktop build is out with 20 early testers and the
repo is newly public. The adoption numbers are days old, so the honest case for it is
the gap it fills and who it is for.

The goal is to put a cloud VM within reach of a far wider set of people than currently
have one. Renting a GPU is easy; renting one *safely* is not, and that gap is what keeps
most people out. Good multi-cloud orchestrators exist for teams, SkyPilot and dstack
among them, but they are control planes you run for a group. What is missing is the
single-user case: someone who wants to rent one GPU for an afternoon without a server to
operate, and without learning which failure modes will cost them. Those failure modes
are consistent: an instance left running over a weekend, or a terminate that destroys
work nobody had synced. Manifold is a local, self-hosted cockpit where the guards
against both are structural rather than a habit you have to build. A single
FastAPI backend owns every action, and the dashboard, desktop app, and MCP server are
thin clients that cannot route around it. Budget caps, a concurrency limit, and idle
auto-termination live in the backend; termination rescues unsaved files before it
destroys anything and refuses if a file could not be saved; nothing on the instance
listens on a non-loopback interface except sshd; every launch, job, command, and agent
tool call lands in one audit log. Lambda Cloud is supported today, Google Cloud VMs are
being implemented now, and AWS EC2, CoreWeave, RunPod, and DigitalOcean are next, so
the safety model follows the user wherever capacity is cheapest.

The part I think matters most is what this does for agents. Connected over MCP, Claude
Code can reach the instances inside Manifold and delegate work to them: hand a long
training or batch job to a GPU instead of running it locally, or spin up a local LLM
subagent to handle image, video, and 3D model generation, work that is outside Claude's
own abilities today. Because every one of the 20 MCP tools goes through the same guarded
backend, an agent renting GPUs hits exactly the same budget wall and the same
save-before-destroy rule a human does. Agentic infrastructure spend is arriving faster
than the safety rails for it, and those rails belong in open, inspectable, self-hosted
software rather than in each user's private scripts. Mock mode runs the entire product
with zero credentials and zero spend, so anyone can audit the guards before trusting
them with a real API key.

Underneath all of this is one bet about interfaces. The existing orchestrators express
their power through YAML and log streams, which is an excellent interface for someone who
already thinks in infrastructure and an opaque one for everyone else. Manifold's wager is
that the same capabilities, surfaced as legible visual state rather than config files and
logs, let people learn infrastructure by doing it safely. The guards are what make that
learning survivable: you can afford to experiment with a system that will not let you
leak a weekend of GPU time or destroy unsynced work.

Near-term: the remaining provider adapters, signed `.dmg`/`.msi` desktop builds, a
public template registry so the generation and fine-tuning recipes are shareable, and a
two-pane job builder where filling in a form assembles the underlying config live beside
it, so the dashboard teaches the declarative layer instead of hiding it.

---

## Q2. How will you use the subscription for your project?

Two things, both on the critical path.

**Development.** I am a solo maintainer and engineer, and Manifold is built with Claude
Code under a strict phased-gate process: every phase runs on its own branch, ships with
tests, and lands an entry in DECISIONS.md recording what was chosen and why. That log
runs to 177KB so far. The discipline is what lets one person maintain a guarded
orchestrator with 480 tests, and it is entirely Claude-assisted. The subscription would fund the next
phases: the multi-provider work (Google Cloud VMs now; AWS EC2, CoreWeave, RunPod, and
DigitalOcean after), signed desktop builds, and a security review of the SSH supervision
and cloud-init paths, which are the highest-risk surfaces in the codebase. Each new
provider has to be brought behind the same guards rather than bolted on beside them,
which is the expensive part.

**Dogfooding the agent surface.** Manifold's MCP server exists so agents can rent GPUs
safely, and Claude Code is the primary client I develop and test that surface against:
`get_skill`, the guarded launch path, capacity waits, save-before-terminate. Every real
agent session exercises the guards in ways unit tests do not, and those findings feed
straight back into the code. The same applies to Autopilot, the in-app agent loop, where
a Claude API brain is the reference implementation others get compared against.

Concretely: faster phase cadence toward a 1.0 anyone can install, and a hardened,
well-documented MCP surface that any agent can use without setting money on fire.

---

## Other info

Manifold is developed openly at https://github.com/Somnora/Manifold under the MIT
license (Python/TypeScript, FastAPI + Next.js + Tauri), with v0.1.0 tagged. It runs
entirely on the user's machine: no hosted service, no telemetry, credentials stay in a
local .env. The full mock demo needs no credentials and spends nothing, which makes it
easy to review.

The project is transparent about being built with AI assistance. DECISIONS.md is a
running log of every non-obvious architectural choice; CLAUDE.md encodes the hard rules
that keep contributions (human or agent) inside the guard rails. I would be glad to walk
through either.

I am the sole maintainer and can be reached at james@somnora.app.
