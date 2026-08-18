# GPU provider survey - August 2026

A dated snapshot: every provider below was graded against Manifold's
provider contract (real VMs; SSH with user keys; user_data or equivalent
boot hook for the sidecar; token-auth HTTP API for catalog/launch/list/
terminate; regions and a price catalog; self-serve indie signup).
Verified against primary docs at survey time; items that could not be
verified say so. Grades decay - re-verify before building anything.

## The board

| Provider | Grade | One-line verdict |
| --- | --- | --- |
| Shadeform (aggregator) | A | One REST integration = ~20-30 clouds of real SSH-able VMs with startup scripts; zero markup per its own docs; SkyPilot ships it as a target, proving the pattern |
| Crusoe Cloud | A | Cleanest direct drop-in; even exposes the capacity endpoint Lambda makes us guess at |
| TensorDock | A- | Cleanest marketplace fit (real VMs, true cloud-init, $5 signup); risk is commercial - acquired by Voltage Park |
| DigitalOcean GPU Droplets | A- | Boring, stable, per-second VM API; lowest signup friction; no A100 |
| Vast.ai | B | Real VMs only via vm:true (thinner inventory); needs hard filters on host reliability scores |
| Voltage Park | B | Live stock counts, good prices; merger churn (now Lightning AI) |
| Nebius | B | Perfect VM semantics but gRPC-only control plane = a genuinely different client |
| AWS EC2 | B- | Fits technically; quota=0 ticket for every fresh account |
| RunPod | C | Highest indie mindshare BUT pods are containers that cannot run Docker - our template/sidecar spine breaks |
| OCI | C | Good API, ticket-gated GPU limits, 8-GPU floors |
| Prime Intellect | C | Aggregator, weaker contract fit than Shadeform |
| Azure | C- | Worst signup friction and API-shape mismatch |
| CoreWeave | F | Kubernetes/Slurm platform, not an instance cloud |
| FluidStack | F | Pivoted to sales-led frontier clusters; no indie path |
| Modal / Together / Replicate / Baseten | F | Serverless/container execution models; no VM+SSH mode (verified) |

## Notable unverifieds (do not build on these without re-checking)

- Shadeform billing increment (its zero-markup claim IS primary-sourced;
  the 6-12% markup figure circulating is third-party SEO, contradicted by
  Shadeform's own pricing page)
- TensorDock v2 API parameter list (reference page blocked scraping) and
  billing granularity
- Crusoe A100 pricing discrepancy and shared-NFS availability
- Voltage Park SSH port semantics and post-merger API continuity

## The strategic finding: SkyPilot

SkyPilot launches and fails over across ~25 infra targets, ships idle
auto-stop, and in March 2026 released an Agent Skill for Claude Code,
Codex, and Cursor - "an agent can launch a GPU" is table stakes now, not
a differentiator. What Manifold has that SkyPilot does not, by its own
docs: guards that are unbypassable by construction for a single-user
install (SkyPilot's client-side policies "lack enforcement capability");
per-action human approval gates on spend; a never-pruned audit ledger;
and termination that rescues unsaved work before it destroys. The moat is
the guardrail layer between an autonomous agent and a credit card - not
breadth. Shadeform is how Manifold gets breadth without betting the
product on it.

## TensorDock warning worth keeping regardless

Its balance drains continuously and servers AUTO-DELETE at $0 - a
silent-destruction path that Manifold's termination-saves-first rule
would have to defend against explicitly if that provider is ever added.
