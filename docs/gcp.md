# Google Cloud on Manifold

Phase 87 wires Manifold's second provider for real: launch GCE GPU
machines with the same guards, the same jobs, and the same teardown safety
as Lambda. Same door, second cloud.

## Sign in - no API key

Manifold never takes a Google key. Authentication is Application Default
Credentials, the SDK's own mechanism:

```bash
gcloud auth login                          # the CLI itself
gcloud auth application-default login      # ADC - what Manifold's SDK uses
```

Both open a browser OAuth into your Google account. Tokens live under
`~/.config/gcloud/`, are revocable from your account page, and expire on
Google's schedule - when they do, Manifold's error says exactly the
command above instead of a traceback. Headless installs can set
`GOOGLE_APPLICATION_CREDENTIALS` to a service-account JSON instead; that
path goes in `.env` like every other secret.

Then tell Manifold which project pays:

```bash
# .env
GCP_PROJECT_ID=your-project-id
```

Anything launched bills that project - which is exactly how GCP credits
get used.

## The quota reality, up front

Fresh GCP projects hold **zero GPU quota** - every metric
(`NVIDIA_L4_GPUS`, `NVIDIA_T4_GPUS`, `GPUS_ALL_REGIONS`...) starts at 0,
and a launch against zero quota fails no matter what any tool does. This
is the single most common reason a first GCP GPU launch fails, so
Manifold shows your quota on the launch form BEFORE the click, and the
refusal after a click links the exact console page and metric to request.
Small requests are usually approved in minutes to hours.

## What is live and what is a label

Zone availability is live: the catalog intersects Manifold's curated
shelf (T4 at ~$0.54/hr through 8x H100) with Google's own
acceleratorTypes listing at request time. Prices are NOT live in v1: they
are dated on-demand list prices, and every entry carries that label -
your bill is Google's meter. The Billing Catalog API is the planned
upgrade.

## Honest limits, v1

- **Scratch-only.** Persistent filesystems are a Lambda feature; GCP
  Filestore is not wired up. Termination's data-rescue can still download
  files to your machine.
- **Boot installs the NVIDIA driver.** GCE Ubuntu images ship none, so
  first boot takes several extra minutes; the GPU-readiness probe holds
  jobs until a container can actually see the card.
- **TPUs are not modelled at all.** TPU VMs are a different API and a
  different execution model (no CUDA, no `docker --gpus`; JAX/XLA); they
  would be their own template family, not a catalog entry.
- Manifold only ever sees machines it launched (label `manifold=true`).
  Your other VMs are invisible to every sweep and untouchable by
  terminate.
