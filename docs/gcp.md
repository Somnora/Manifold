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

## Data volumes: a persistent home for a GCP box

Without one, a GCP instance is scratch-only: everything on it dies with the
box (termination's data-rescue can still download files to your machine, but
that is a net, not a home). A **data volume** is a Compute Engine Persistent
Disk that Manifold creates, attaches, and mounts at `/lambda/nfs/<name>` -
the same path a Lambda filesystem lands on, so jobs, `{persistent}` template
mounts, `sync_outputs`, the Files browser and relative paths all work
identically.

Create one on the Storage page (name, zone, size), then pick it as the
volume on the launch form. Agents can read the list with the MCP tool
`list_volumes` and pass a name as `launch_gpu`'s `filesystem`; creating and
deleting is deliberately a human action.

### A volume is ZONAL, and that is the trap

A Persistent Disk lives in one zone and can only attach to an instance in
that same zone. GPU capacity varies by zone - `us-central1-a` may have L4s
today and `us-central1-b` may not - so **it is possible to end up with your
data in a zone where the GPU you want is unavailable**. When that happens
Manifold refuses rather than quietly moving the zone and launching without
your data; the launch form says which GPU is missing where, and the choice
of what to change is yours.

There is no resize and no snapshot in this version. Snapshot-and-restore
into another zone is the standard escape hatch and is not built here; today
the ways out are copying the data off over the instance, or creating a
second volume in the zone you want.

### What it costs

A volume bills for its **provisioned** size from the moment it exists,
attached or not - unlike a Lambda filesystem, which bills for what it
holds. Manifold shows Google's dated list price per volume and does not
pretend to meter it. A volume you stopped using is a volume still billing:
delete it.

Manifold never reports a `bytes_used` for a volume, anywhere. Nothing
outside the instance can read a detached disk, and a `0` there would claim
it is empty. To see what is on one, launch a box with it attached and use
the Files panel.

### Every way a volume can refuse, and what it means

Manifold would rather fail a launch than come up with a path that looks
persistent and is not. These are the refusals you can actually hit:

- **"attached to \<instance\>"** - a disk attaches to one instance at a
  time. Terminate the holder (Manifold saves its files first, and the volume
  survives) and launch again. If Google reports that instance as *stopped*
  rather than deleted, it still holds its disks: delete it, or detach the
  disk in the console.
- **"Zone mismatch"** - the volume is in another zone. Launch there, or go
  without it.
- **"already holds a filesystem that Manifold has no record of writing"** -
  the disk was formatted outside Manifold, or this install's database is not
  the one that created it. Manifold will not mount it (a job pointed at
  someone else's data corrupts it) and will not format it (that destroys
  it). Attaching pre-existing disks is not supported.
- **"recorded as formatted ... but the disk holds no filesystem"** - a
  format was interrupted. Manifold records the format *before* it runs it,
  precisely so this state is detectable, and it refuses to run mkfs twice.
  Because that record is written once, such a volume provably never held any
  of your data: delete it and create another.
- **"already exists with files in it and nothing mounted there"** -
  something wrote to `/lambda/nfs/<name>` while the volume was not mounted.
  Mounting would hide those files, and they are evidence. Look at them on
  the instance first.
- **"is not a mounted filesystem"** on a job or a sync - the box is up but
  the volume is not mounted (still coming up, or a repair failed). The job
  did not run and nothing was written; nothing is lost. Manifold refuses
  every write to that path rather than letting it land on the boot disk,
  which is deleted with the instance.

### What it is not

A volume is not a "filesystem" anywhere in Manifold: it does not appear
under `/filesystems`, it does not back the Storage page's file browser, and
it never carries a size-used it cannot know. Those are all things a Lambda
filesystem can answer and a Persistent Disk cannot, and giving a volume a
plausible-looking value for them would be worse than not showing it.

## Honest limits, v1

- **One volume per instance**, and only on GCP - `extra_filesystems` is
  refused there. A Persistent Disk attaches to one machine at a time.
- **No resize, no snapshots, no attaching a disk Manifold did not create.**
- **The volume is not in `/etc/fstab`.** Manifold re-mounts it if it finds
  an active box with it unmounted; it never formats on that path.
- **Boot installs the NVIDIA driver.** GCE Ubuntu images ship none, so
  first boot takes several extra minutes; the GPU-readiness probe holds
  jobs until a container can actually see the card.
- **TPUs are not modelled at all.** TPU VMs are a different API and a
  different execution model (no CUDA, no `docker --gpus`; JAX/XLA); they
  would be their own template family, not a catalog entry.
- Manifold only ever sees machines it launched (label `manifold=true`).
  Your other VMs are invisible to every sweep and untouchable by
  terminate.
