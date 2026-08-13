# Chapter 17 — Kubernetes: a local learning sandbox

**Last updated:** 2026-08-13 · **Status:** 🚧 in progress — Levels 1–2 built (cluster + database); Levels 3–6 planned

> **This is not how Thyme is deployed.** Production stays exactly where it is:
> frontend on Vercel, backend on Render, Postgres on Neon
> ([Ch 18 / deployment.md](../deployment.md)). Everything in this chapter runs on
> a throwaway [`kind`](https://kind.sigs.k8s.io/) cluster on the laptop, costs
> nothing, touches no cloud account, and can be deleted with one command.

**Why we did this.** The honest answer is in
[DECISIONS.md → 2026-08-07](../DECISIONS.md): Thyme is a *bad* Kubernetes
candidate for production — it's already right-sized on managed platforms, and
self-hosting Postgres (StatefulSet + PVC + backups + upgrades) is strictly more
operational risk than Neon, not less. K8s exists to solve problems this app
doesn't have.

But it's an unusually *good* Kubernetes candidate for **learning**, and that was
the real goal. The trick we settled on was to **manufacture the condition**: you
can't wait until you've built a system big enough to justify heavyweight tooling
before you learn the tooling. Instead, take a small app that happens to have all
the interesting properties — three tiers, a stateful database, migrations on
boot, real secrets, and a genuine build-time configuration gotcha — and learn on
that.

**The rule we set:** raw manifests first, Helm later. Templating a thing you
don't understand teaches you the template, not the thing.

---

## 17.1 Mental model — declare the destination, not the route

> **You don't tell Kubernetes what to do; you tell it what you want to be true.**
> A manifest is a record of desired state. Controllers — control loops running in
> the cluster — continuously compare desired state against actual state and act to
> close the gap. "Delete this pod" doesn't remove the pod so much as it schedules
> its replacement, because something still wants one to exist.

> **`kind` is "Kubernetes IN Docker".** The whole cluster — control plane and node
> — runs as containers inside Docker Desktop. So the containers we build in
> [Ch 16](13-containerization.md) run inside containers. It's a full, real
> Kubernetes API; it just lives on your laptop and dies when you say so.

The vocabulary, mapped to what we actually wrote:

| Object | One-line job | Ours |
|---|---|---|
| **Namespace** | a scope for names, and a unit of teardown | `thyme` |
| **Secret** | key/value config, base64-at-rest, mountable as env | `postgres-secret` |
| **Service** | a stable name/address in front of pods | `postgres` (headless) |
| **StatefulSet** | pods with *stable identity + own storage* | `postgres` → pod `postgres-0` |
| **PVC** | a claim on durable disk, outliving the pod | `data-postgres-0` (auto-minted) |
| Deployment | interchangeable, replaceable pods | Levels 3–4 (backend, frontend) |
| Ingress | HTTP routing from outside the cluster | Level 5 |

---

## 17.2 The curriculum, and where we are

| Level | Tier | Objects | Status |
|---|---|---|---|
| 1 | Cluster | `kind create cluster --name thyme` | ✅ |
| 2 | **Database** | Secret, headless Service, StatefulSet (+PVC) | ✅ |
| 3 | **Backend** | Deployment, Service, ConfigMap/Secret, migrations on boot | 🚧 |
| 4 | **Frontend** | Deployment, Service | 🚧 |
| 5 | **Ingress** | `thyme.local` → frontend, `/api` → backend | 🚧 |
| 6 | **Helm** | the same three tiers as a chart | 🚧 |

Files: [k8s/README.md](../../k8s/README.md) (the runbook),
[namespace.yaml](../../k8s/namespace.yaml),
[database/postgres-secret.yaml](../../k8s/database/postgres-secret.yaml),
[database/postgres-service.yaml](../../k8s/database/postgres-service.yaml),
[database/postgres-statefulset.yaml](../../k8s/database/postgres-statefulset.yaml).

Level 5 isn't cosmetic — it's the level that *solves a real problem* we already
know we have. See §17.5.

---

## 17.3 Level 1 — the cluster

```bash
kind create cluster --name thyme
kubectl get nodes          # thyme-control-plane   Ready
kubectl config current-context   # kind-thyme
```

`kind` writes a kubeconfig context and switches to it. Two habits worth forming
immediately: check `current-context` before running anything destructive, and
remember that every command below needs `-n thyme` (§17.7).

Tearing down is the reason this is a safe place to experiment:

```bash
kind delete cluster --name thyme    # cluster, volumes, data — all of it
```

---

## 17.4 Level 2 — Postgres, the hard tier first

We deliberately started with the database, because it's where Kubernetes stops
being "docker run with YAML" and the interesting concepts appear.

### Namespace: a unit of teardown

```yaml
kind: Namespace
metadata:
  name: thyme
  labels: { app.kubernetes.io/part-of: thyme }
```

Everything lives in `thyme`, so the whole app can be listed, inspected or deleted
as a unit, and nothing collides with `kube-system`.

### Secret: how config reaches a container

```yaml
kind: Secret
metadata: { name: postgres-secret, namespace: thyme }
type: Opaque
stringData:
  POSTGRES_USER: thyme
  POSTGRES_PASSWORD: thyme-dev-password
  POSTGRES_DB: thyme
```

`stringData` lets you write plaintext and have the API server base64-encode it —
`data` would require you to encode by hand. The StatefulSet consumes the whole
Secret as environment:

```yaml
envFrom:
  - secretRef: { name: postgres-secret }
```

The official Postgres image reads those three variables **on first boot only** to
create the superuser and database. One Secret is also the single source of truth
that Level 3's backend will use to compose its `DATABASE_URL` — same credentials,
declared once.

> ⚠️ **Committing this Secret is a sandbox-only decision**, and the manifest says
> so in its own comments. A Secret is *encoded*, not *encrypted*; anyone with the
> file (or `kubectl get secret -o yaml`) has the password. Production means Sealed
> Secrets, External Secrets Operator, or a cloud secret manager — never a
> committed manifest.

### Headless Service: a name instead of an IP

```yaml
spec:
  clusterIP: None          # ← "headless"
  selector: { app: postgres }
  ports: [{ name: postgres, port: 5432, targetPort: 5432 }]
```

Pod IPs change on every restart, so nothing should ever connect to one. A Service
gives a stable DNS name: `postgres` inside the namespace, or
`postgres.thyme.svc.cluster.local` from anywhere in the cluster.

`clusterIP: None` means **no load-balancer virtual IP** — DNS resolves straight to
the pod. For a single stateful database that's what you want: a normal Service
would put a proxy in front of a thing that must be addressed directly, and with
multiple replicas you'd want to reach *a specific* member (`postgres-0`), not a
random one. Headless + StatefulSet is the conventional pairing.

### StatefulSet: identity and storage that outlive the pod

```yaml
kind: StatefulSet
spec:
  serviceName: postgres     # must match the headless Service
  replicas: 1
  template:
    spec:
      containers:
        - name: postgres
          image: pgvector/pgvector:pg16
```

**Why not a Deployment?** A Deployment's pods are interchangeable: random names,
no stable storage relationship, replaced freely. That's correct for stateless web
tiers and wrong for a database. A StatefulSet gives:

- a **stable name** — `postgres-0`, and `postgres-1` would always be `postgres-1`;
- a **stable PVC per replica**, reattached to the pod with that identity;
- **ordered** creation and deletion (matters for replicated stores).

**Why the pgvector image?** The journal RAG migration runs `CREATE EXTENSION
vector` ([Ch 12](08-journal-rag.md)); stock `postgres:16` doesn't have it
installed and the migration fails. Same reasoning as the
[CI service container](12-ci-pipelines.md#153-the-tricky-part--a-real-postgres-inside-ci) —
one requirement showing up in two environments.

```yaml
  volumeClaimTemplates:
    - metadata: { name: data }
      spec:
        accessModes: ["ReadWriteOnce"]
        resources: { requests: { storage: 1Gi } }
```

`volumeClaimTemplates` is the StatefulSet-only feature that mints a PVC per
replica — here `data-postgres-0`. `kind` ships a default `local-path`
StorageClass, so the volume is provisioned automatically with no cloud disk
involved.

```yaml
          env:
            - name: PGDATA
              value: /var/lib/postgresql/data/pgdata
          volumeMounts:
            - { name: data, mountPath: /var/lib/postgresql/data }
```

**The `PGDATA` subdirectory trick.** Mounting a volume *directly* at
`/var/lib/postgresql/data` can trip `initdb`, which insists on an empty
directory — and many provisioners leave a `lost+found` behind. Pointing `PGDATA`
at a subdirectory of the mount sidesteps it. This is the kind of detail that
looks arbitrary until it costs you an hour of `CrashLoopBackOff`.

```yaml
          readinessProbe:
            exec: { command: ["pg_isready", "-U", "thyme", "-d", "thyme"] }
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            exec: { command: ["pg_isready", "-U", "thyme", "-d", "thyme"] }
            initialDelaySeconds: 15
            periodSeconds: 20
```

The two probes answer different questions, and confusing them causes outages:

- **Readiness — "should traffic go here?"** Fails → the pod is removed from
  Service endpoints, but left alone. This is what stops the backend connecting
  during the ~seconds Postgres spends starting up.
- **Liveness — "is this process wedged?"** Fails → the kubelet **kills the
  container**. Hence the longer delay and period: an aggressive liveness probe on
  a slow-starting database is a restart loop generator.

Same split as our own HTTP endpoints, `/health` vs `/health/ready`
([Ch 8](03-observability.md)) — the concept is the framework's, not Kubernetes'.

```yaml
          resources:
            requests: { cpu: "100m", memory: "256Mi" }
            limits:   { memory: "512Mi" }
```

**Requests** are for *scheduling* (find a node with this much free) and
**limits** are for *enforcement*. Deliberately no CPU limit: exceeding a CPU limit
throttles a process (slow, hard to diagnose), while exceeding a memory limit gets
it OOM-killed. Requesting CPU without capping it is the common recommendation, and
it's what we did.

### Bringing it up

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/database/
kubectl -n thyme rollout status statefulset/postgres
kubectl -n thyme exec -it postgres-0 -- psql -U thyme -d thyme -c '\conninfo'
```

---

## 17.5 The tricky part — two things the manifests can't hide

### 1 · Storage outlives almost everything

Deleting the pod keeps the data (that's the point). Deleting the *StatefulSet*
**also** keeps the PVC — Kubernetes will not throw away your disk because you
removed a controller. So "delete and re-apply" does not give you a fresh
database, and a re-created pod comes back with the old credentials baked into
`PGDATA` (remember: `POSTGRES_PASSWORD` is read on *first* boot only).

That means editing the Secret and re-applying appears to do nothing. To truly
reset:

```bash
kubectl -n thyme delete statefulset postgres
kubectl -n thyme delete pvc data-postgres-0     # ← the step people miss
kubectl apply -f k8s/database/
```

### 2 · The build-time frontend gotcha is what Level 5 is *for*

From [Ch 16 §16.4](13-containerization.md#164-the-tricky-part--next_public_-is-baked-in-at-build-time):
`NEXT_PUBLIC_API_URL` is compiled into the browser bundle, and **the browser** —
not the frontend pod — calls the API. So the natural-looking in-cluster value:

```
NEXT_PUBLIC_API_URL=http://backend:8000     # ✗ never resolves in your browser
```

…cannot work. `backend` is cluster-internal DNS; your browser is outside.

The fix is one **Ingress** giving both tiers a single browser-reachable origin:

```
                    ┌─ /      → Service frontend:3000
thyme.local ──────► │
   (/etc/hosts)     └─ /api   → Service backend:8000
```

Then the baked-in value becomes `http://thyme.local/api` (or, better, a relative
`/api`) — one origin, no CORS, and the image stops being environment-specific.
Working through this in a sandbox is the best possible way to *understand* the
gotcha rather than memorise it.

---

## 17.6 How to run, inspect and debug

Level 2, from nothing:

```bash
kind create cluster --name thyme
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/database/
kubectl -n thyme rollout status statefulset/postgres
```

The debugging loop that answers most questions, in order:

```bash
kubectl -n thyme get pods -o wide          # STATUS, RESTARTS — start here
kubectl -n thyme describe pod postgres-0   # Events at the bottom = why it won't start
kubectl -n thyme logs postgres-0           # what the process itself said
kubectl -n thyme logs postgres-0 --previous  # the crashed instance's logs
kubectl -n thyme get events --sort-by=.lastTimestamp
kubectl -n thyme exec -it postgres-0 -- psql -U thyme -d thyme
kubectl -n thyme get pvc,secret,svc,statefulset
```

Rule of thumb: **`describe` for scheduling/mounting/probe problems, `logs` for
application problems.** `Pending` is almost always a scheduling or PVC issue
(describe); `CrashLoopBackOff` is almost always the app (logs `--previous`).

Reach the database from your laptop:

```bash
kubectl -n thyme port-forward svc/postgres 5433:5432
psql postgresql://thyme:thyme-dev-password@localhost:5433/thyme
```

Port 5433, not 5432, so it doesn't fight the Postgres you already run locally.

Getting images in (Levels 3–4 will need this):

```bash
docker build -t thyme-backend:dev backend
kind load docker-image thyme-backend:dev --name thyme
```

`kind load` copies the local image straight into the node — no registry, no auth,
no `imagePullSecrets`. Pair it with `imagePullPolicy: IfNotPresent`, or the
kubelet will try to pull `thyme-backend:dev` from Docker Hub and fail.

---

## 17.7 Gotchas

- **`-n thyme` on everything.** Without it you're querying `default` and
  everything looks empty. `kubectl config set-context --current --namespace=thyme`
  once, and stop typing it.
- **Deleting a StatefulSet keeps its PVC** (§17.5). This is the single most
  confusing behaviour in Level 2.
- **`POSTGRES_PASSWORD` only applies on first boot.** Changing the Secret does
  nothing to an initialised volume.
- **A changed Secret doesn't reach a running pod's env.** Env vars are set at
  container start; you need a restart (`kubectl -n thyme rollout restart
  statefulset/postgres`). Mounted-as-file secrets *do* update — env ones don't.
- **`kind load` after every rebuild.** Rebuilding an image locally does not
  update the copy inside the cluster, and a same-tag image won't be re-pulled.
  Symptom: your fix "doesn't apply".
- **`latest` from GHCR is `linux/amd64` only** ([Ch 15](12-ci-pipelines.md)) — on
  Apple Silicon build locally and `kind load` instead.
- **`kubectl apply` on an immutable field fails.** Much of a StatefulSet's spec
  (notably `volumeClaimTemplates` and `selector`) can't be changed in place;
  delete and recreate the controller.
- **Probes run *inside* the container**, so `pg_isready` must exist there and the
  `-U`/`-d` flags must match the Secret. A probe with the wrong user fails
  forever, and liveness will then kill the pod on a loop.
- **Docker Desktop's memory limit is the cluster's ceiling.** A too-small
  allocation shows up as unexplained `Pending` pods and evictions.
- **Never point this at Neon.** The sandbox's database is a throwaway; the
  connection string in that Secret must stay local.

---

## 17.8 Future enhancements

The next four levels, each with the one idea it's meant to teach:

- **Level 3 — backend Deployment + Service:** ConfigMap vs Secret, and where
  migrations belong. The entrypoint currently migrates on boot
  ([Ch 16 §16.6](13-containerization.md#166-gotchas)), which is exactly the thing
  a K8s `Job` or init container exists to fix once there's more than one replica.
  Also: wire the app's own `/health` and `/health/ready` to real probes.
- **Level 4 — frontend Deployment + Service:** trivial by comparison, and the
  point is noticing *why* it's trivial (stateless, interchangeable, no storage).
- **Level 5 — Ingress:** `thyme.local` + a `/etc/hosts` entry, the fix in §17.5.
  Needs a `kind` cluster created with `extraPortMappings` and an ingress
  controller (nginx).
- **Level 6 — Helm:** only now, with three hand-written tiers to compare against,
  templating earns its keep — values per environment, one `helm upgrade`, and a
  real answer to "what would I have to repeat by hand?"

Later, if the curiosity holds: an HPA (to watch autoscaling react), resource
tuning under load, a backup `CronJob` for the PVC, and a `kustomize` overlay as a
counterpoint to Helm.
