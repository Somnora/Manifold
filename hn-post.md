# Show HN draft: Manifold

Submit the GitHub URL (https://github.com/Somnora/Manifold), then post the
comment below as the first reply within a minute or two. That is the standard
Show HN pattern: the link is the submission, the story goes in a comment.

Do not post this until you have actually installed dstack. The comment below
names it, and someone will ask you a comparison question you cannot bluff.

---

## Title options (HN caps titles at 80 characters)

1. `Show HN: Manifold – a desktop app for renting GPUs that refuses to lose your files`  (too long, 82)
2. `Show HN: Manifold – single-user GPU orchestration where the guards can't be bypassed`  (too long, 84)
3. `Show HN: Manifold – a desktop cockpit for renting cloud GPUs safely`
4. `Show HN: Manifold – GPU orchestration for one person, with the guards built in`

My pick is #4. It signals the niche (one person, not a team) in the first
four words, which is the honest differentiator against the incumbents.

---

## First comment

I built this after the third time I left a GPU instance running overnight.

Up front, because it is the first thing anyone here will ask: SkyPilot and
dstack already exist, they are more mature than this, they cover far more
clouds, and if you are a team running multi-cloud AI infrastructure you should
probably use one of them instead. Lambda themselves point at dstack. I am not
claiming to have found an empty field.

What I wanted and could not find was a version of this built for one person,
where the safety rails are structural rather than conventions I have to
remember. Manifold is a local FastAPI backend that owns every action against
the provider API, with a dashboard, a desktop app, and an MCP server that are
all thin clients of it. Three things fall out of that design that I have not
seen elsewhere:

**Terminate refuses to destroy unsaved work.** Shutting down an instance first
rescues its ephemeral files according to a data-safety policy, and if a file
could not be saved, the termination does not happen. There is exactly one
explicit "burn it" override, and it is a separate argument you have to pass on
purpose. The usual answer to this problem is "use persistent volumes
correctly," which is fine advice that does not help at 2am.

**The guards bind agents, not just me.** It exposes 20 MCP tools, so Claude
Code can reach the instances and delegate work to them: hand off a long batch
job, or spin up a local model for image and video generation. Every one of
those tools goes through the same backend, so an agent renting GPUs hits the
same budget cap and the same save-before-terminate rule I do. The MCP server is
AST-enforced as HTTP-only so it cannot reach around the orchestrator. There is
a SkyPilot MCP server out there, but it wraps the CLI, which means an agent
holding it can spend without limit. That difference is most of why I kept
building.

**Nothing on the instance listens on the network.** GPU instances expose sshd
and nothing else. Model servers bind to loopback and are reached through the
managed SSH connection, and the only public face is an OpenAI-compatible proxy
on my own machine at localhost:8000/v1, which any existing tool can point at.
The multi-cloud orchestrators solve serving with public gateways and endpoints,
which is the right call for a team and the wrong one for me.

The thing I keep coming back to is that these tools express their power through
YAML and log streams, which is a fine interface once you already think in
infrastructure and an opaque one before that. I would rather the state of the
system be visible: what is running, what it has cost so far, what happens next.
That is a UI opinion more than a technical one, and it is the direction I am
taking this.

Beyond that it is job templates rather than shell sessions: vllm-serve,
whisper-batch, axolotl-finetune, lora-merge, sdxl-generate. Jobs survive
backend restarts and can manage their own instance end to end, meaning rent a
GPU, run, sync outputs, terminate. The distillation loop is templates the whole
way down, so a teacher model writes a training set, a student LoRA trains on
it, the adapter merges into a standalone model, and you serve the result.

There is a mock mode that runs the entire product against a simulated cloud:
full catalog, launches, jobs, terminals, telemetry, zero credentials and zero
spend. Clone it and click through the whole thing in about 90 seconds without
giving anyone a credit card. That also means contributors can work on it
without a Lambda account.

Lambda Cloud is what works today. Google Cloud VMs are in progress, with EC2,
CoreWeave, RunPod, and DigitalOcean after.

On the obvious question: this is built with Claude Code, and I am not going to
pretend otherwise. It ships with 480 tests that all run against mocks, every
phase is gated on those tests passing, and every non-obvious architectural
decision is written down in DECISIONS.md with the alternatives I rejected. That
file is 177KB and is probably the most honest view of how it was actually
built. Happy to argue about whether that is enough process, because I am
genuinely unsure where the line is.

MIT licensed. I would especially like to hear from anyone with opinions on the
SSH supervision or the cloud-init generation, which are the two parts I
consider highest risk.

---

## Prepared answers

**"Why not just use dstack / SkyPilot?"**
You probably should, if you are a team. They cover more clouds, they are more
mature, and dstack in particular is close to what I built. The difference is
posture: they are control planes you run for a group, with public endpoints and
volumes you are expected to configure correctly. Manifold is a single-user
desktop app where the failure modes I personally kept hitting (forgetting an
instance, destroying unsynced output) are enforced in the backend and cannot be
bypassed by any client, including an AI agent. Narrower scope, stricter rails.

**"Isn't this just a wrapper around the Lambda API?"**
The API calls are the easy part. The parts that took the time are the SSH
supervision that survives backend restarts and re-adopts running instances, the
save-before-terminate interlock, and making sure no client can reach around the
guards. Roughly 13k lines and 480 tests, and very little of it is HTTP calls.

**"Why one provider?"**
Because getting the guards right against one provider was already the hard
part, and each new provider has to be brought behind the same interlocks rather
than bolted on beside them. Google Cloud is in progress now.

**"AI-generated slop?"**
Fair prior. Read DECISIONS.md and the test suite and tell me what you think.
That is a real invitation, not a deflection. If the process is not enough I
would rather hear it here than find out later.
