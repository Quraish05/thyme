# Chapter 17 — Kubernetes: a local learning sandbox

**Last updated:** 2026-08-14 · **Status:** ✅ all six levels built (cluster → database → backend → frontend → Ingress → Helm)

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
| **Deployment** | interchangeable, replaceable pods | `backend`, `frontend` |
| **ConfigMap** | non-secret config, mountable as env | `backend-config` |
| **Ingress** | HTTP routing from outside the cluster | `thyme.local` → frontend + `/api` → backend |

---

## 17.2 The curriculum, and where we are

| Level | Tier | Objects | Status |
|---|---|---|---|
| 1 | Cluster | `kind create cluster --name thyme` | ✅ |
| 2 | **Database** | Secret, headless Service, StatefulSet (+PVC) | ✅ |
| 3 | **Backend** | Deployment, Service, ConfigMap/Secret, migrations on boot | ✅ |
| 4 | **Frontend** | Deployment, Service | ✅ |
| 5 | **Ingress** | `thyme.local` → frontend, `/api` → backend | ✅ |
| 6 | **Helm** | the same tiers as one parameterized chart | ✅ |

Files: [k8s/README.md](../../k8s/README.md) (the runbook),
[namespace.yaml](../../k8s/namespace.yaml),
[database/](../../k8s/database/) (Secret, Service, StatefulSet),
[backend/](../../k8s/backend/) (ConfigMap, Secret, Deployment, Service),
[frontend/](../../k8s/frontend/) (Deployment, Service),
[ingress.yaml](../../k8s/ingress.yaml),
[kind-cluster.yaml](../../k8s/kind-cluster.yaml),
[helm/thyme/](../../k8s/helm/thyme/) (the Level 6 chart).

Level 5 isn't cosmetic — it's the level that *solves a real problem* we already
know we have. See §17.9.

---

## 17.3 Level 1 — the cluster

```bash
kind create cluster --name thyme
kubectl get nodes          # thyme-control-plane   Ready
kubectl config current-context   # kind-thyme
```

`kind` writes a kubeconfig context and switches to it. Two habits worth forming
immediately: check `current-context` before running anything destructive, and
remember that every command below needs `-n thyme` (§17.11).

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

## 17.5 Level 3 — the backend

The backend is a **Deployment**, not a StatefulSet: it holds no state (all of
that lives in Postgres), so its pods are interchangeable and replaceable — which
is exactly what a Deployment gives you. Its config splits along the line drawn in
Level 2:

```yaml
envFrom:
  - configMapRef: { name: backend-config }   # non-secret: env, CORS, PORT
  - secretRef:    { name: backend-secret }    # DATABASE_URL, SECRET_KEY
```

The **ConfigMap vs Secret** split is the lesson: things you'd happily print in a
log (`ENVIRONMENT`, `CORS_ORIGINS`, `PORT`) go in the ConfigMap; credentials go
in the Secret. Both arrive as environment variables, so `app/core/config.py`
never knows the difference.

The one value that ties the tiers together is the connection string, pointing at
the Level 2 Service by its DNS name — not a pod IP:

```yaml
DATABASE_URL: postgresql+asyncpg://thyme:thyme-dev-password@postgres:5432/thyme
```

`postgres` resolves to the headless Service from §17.4; `thyme`/`thyme-dev-password`
are the same credentials declared once in `postgres-secret`.

### Migrations on boot, and the probe that makes room for them

The image's entrypoint runs `alembic upgrade head` *before* uvicorn starts
([Ch 16 §16.2](13-containerization.md#the-entrypoint-migrate-then-exec)). On a
fresh cluster that means the pod isn't answering HTTP for the first few seconds
while it builds the whole schema (and runs `CREATE EXTENSION vector`). A
**startup probe** buys that time without the liveness probe killing the pod
mid-migration:

```yaml
startupProbe:                    # up to 30 × 5s = 150s for migrations
  httpGet: { path: /api/v1/health, port: http }
  periodSeconds: 5
  failureThreshold: 30
readinessProbe: { httpGet: { path: /api/v1/health, port: http }, periodSeconds: 10 }
livenessProbe:  { httpGet: { path: /api/v1/health, port: http }, periodSeconds: 20 }
```

Once the startup probe passes, readiness and liveness take over — the three-probe
pattern the app's own health split ([Ch 8](03-observability.md)) was built for.

`imagePullPolicy: IfNotPresent` is mandatory here: the image was `kind load`ed,
not pushed to a registry, so the kubelet must use the node-local copy instead of
trying (and failing) to pull `thyme-backend:dev` from Docker Hub.

### Bring it up

```bash
docker build -t thyme-backend:dev backend/
kind load docker-image thyme-backend:dev --name thyme
kubectl apply -f k8s/backend/
kubectl -n thyme rollout status deployment/backend
kubectl -n thyme exec deploy/backend -- \
  python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/api/v1/health').read())"
# → b'{"status":"ok","version":"0.1.0"}'  — and psql shows 15 tables + the vector extension
```

> **Watch it self-heal.** On first boot the backend often `CrashLoopBackOff`s a
> few times: its entrypoint tries to migrate the instant it starts, which can be
> *before* Postgres is accepting connections, so `alembic` fails and `set -e`
> exits non-zero. Kubernetes restarts it, and once Postgres is ready the migration
> succeeds. That's the declarative model doing its job — but the clean fix is an
> **initContainer** that blocks on `pg_isready` before the app container runs, so
> the restarts never happen (a listed future enhancement, and the same problem
> [Ch 16 §16.6](13-containerization.md#166-gotchas) flags for multi-replica boots).

---

## 17.6 Level 4 — the frontend

Level 4 is deliberately anticlimactic, and noticing *why* is the point. The
frontend is stateless, so it's a plain Deployment + Service with no ConfigMap, no
Secret, and no storage — everything interesting about it was already decided at
**build** time (§17.9, and [Ch 16 §16.4](13-containerization.md#164-the-tricky-part--next_public_-is-baked-in-at-build-time)).

```bash
docker build --build-arg NEXT_PUBLIC_API_URL=http://thyme.local -t thyme-frontend:dev frontend/
kind load docker-image thyme-frontend:dev --name thyme
kubectl apply -f k8s/frontend/
```

The `--build-arg` is the whole story: the browser talks to the API, so the URL is
compiled into the bundle now, pointing at the Ingress host we're about to create.
The probes hit `/` (the standalone server answers it once up); resources are tiny.
That's it — stateless tiers are easy, which is exactly what makes the database's
complexity in Level 2 worth dwelling on.

---

## 17.7 Level 5 — the Ingress (and the payoff)

This is the level that turns three separate Services into one app your browser can
open, and it's the concrete fix to the build-time gotcha (§17.9).

**First, the cluster needs a door.** A default `kind` cluster has no way for
`localhost:80` to reach the Ingress controller, so Level 5 starts by *recreating*
the cluster from a config that adds host port-mappings and the label the nginx
controller schedules onto:

```yaml
# k8s/kind-cluster.yaml
nodes:
  - role: control-plane
    kubeadmConfigPatches:
      - |
        kind: InitConfiguration
        nodeRegistration:
          kubeletExtraArgs: { node-labels: "ingress-ready=true" }
    extraPortMappings:
      - { containerPort: 80,  hostPort: 80 }
      - { containerPort: 443, hostPort: 443 }
```

```bash
kind delete cluster --name thyme
kind create cluster --name thyme --config k8s/kind-cluster.yaml
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml
kubectl wait -n ingress-nginx --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller --timeout=150s
```

(Recreating is cheap — everything is declarative, so you just re-`kind load` the
images and re-`apply` all the manifests afterward.)

**Then the routing rule** is a single host with two paths — nginx matches the
longest prefix, so `/api/v1/health` goes to the backend and everything else to the
frontend:

```yaml
# k8s/ingress.yaml
rules:
  - host: thyme.local
    http:
      paths:
        - { path: /api, pathType: Prefix, backend: { service: { name: backend,  port: { number: 8000 } } } }
        - { path: /,    pathType: Prefix, backend: { service: { name: frontend, port: { number: 3000 } } } }
```

This is what makes the frontend's baked-in `NEXT_PUBLIC_API_URL=http://thyme.local`
correct: the browser loads the page from `thyme.local` and calls the API on the
*same* host, so the Ingress routes `/api` to the backend. No CORS, no in-cluster
DNS in the browser — the gotcha §17.9 describes, dissolved.

**Point the hostname at your machine** (once; needs `sudo`) and open it:

```bash
echo "127.0.0.1 thyme.local" | sudo tee -a /etc/hosts
open http://thyme.local          # the Thyme login page, talking to the cluster
```

Verify the full path from the terminal without even editing `/etc/hosts`
(`--resolve` fakes the DNS for one request):

```bash
curl --resolve thyme.local:80:127.0.0.1 http://thyme.local/api/v1/health
# → {"status":"ok","version":"0.1.0"}     browser → Ingress → backend → Postgres
curl --resolve thyme.local:80:127.0.0.1 http://thyme.local/
# → HTML with <title>Thyme</title>        browser → Ingress → frontend
```

That's all three tiers — from Docker images — running in Kubernetes, reached the
way a real user would. Everything after this (Level 6) is about *repeating* it
with less typing.

---

## 17.8 Level 6 — Helm

Five levels of hand-written YAML make the case *for* Helm better than any
tutorial could: you can now see exactly what it removes. The chart at
[k8s/helm/thyme/](../../k8s/helm/thyme) packages the same objects, with every
value the raw manifests hard-coded lifted into one
[values.yaml](../../k8s/helm/thyme/values.yaml).

> **A chart is templates + values.** `helm install` renders the templates against
> the values and applies the result. The win isn't magic — it's that "what
> changes between environments" (image tags, replica counts, the Ingress host, DB
> credentials) becomes *one file*, not a hunt through ten manifests.

The layout mirrors the tiers you already built by hand:

```
k8s/helm/thyme/
  Chart.yaml            # name + version metadata
  values.yaml           # the single config surface
  templates/
    _helpers.tpl        # common labels, defined once
    database.yaml       # Secret + headless Service + StatefulSet
    backend.yaml        # ConfigMap + Secret + Deployment + Service
    frontend.yaml       # Deployment + Service
    ingress.yaml        # guarded by an if/end on ingress.enabled
    NOTES.txt           # post-install instructions
```

Two ideas earn their keep immediately:

- **Credentials declared once.** The DB user/password/name live in `values.yaml`,
  and the backend's `DATABASE_URL` is *composed* from them in the template —
  `…://{{ .Values.database.user }}:{{ .Values.database.password }}@postgres:5432/{{ .Values.database.name }}`.
  The raw manifests repeated the password in two files and *hoped* they matched;
  the chart makes drift impossible.
- **Conditional resources.** `ingress.yaml` is wrapped in an
  `if .Values.ingress.enabled` guard, so the same chart deploys with or without an
  Ingress (fall back to `port-forward`). That guard also taught a lesson: Helm
  parses template directives *inside `#` comments*, so a stray `if` in a comment
  is a real parse error — which `helm lint` catches before you ever install.

### Install it — one command for the whole app

```bash
kind load docker-image thyme-backend:dev thyme-frontend:dev --name thyme
helm install thyme k8s/helm/thyme --namespace thyme --create-namespace
```

That single `helm install` replaces the entire §17.3–§17.7 `kubectl apply`
sequence. The deployed objects are identical, so you verify exactly as before:

```bash
helm -n thyme list                 # thyme   deployed   thyme-0.1.0
curl --resolve thyme.local:80:127.0.0.1 http://thyme.local/api/v1/health
# → {"status":"ok","version":"0.1.0"}
```

`helm upgrade thyme k8s/helm/thyme` re-renders and applies only the diff;
`helm uninstall thyme` removes every object in the release at once. That
lifecycle — install / upgrade / roll back / uninstall as *one unit* — is the
thing raw manifests never gave us, and the whole reason Helm exists.

---

## 17.9 The tricky part — two things the manifests can't hide

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

## 17.10 How to run, inspect and debug

The full stack, from nothing, lives in [k8s/README.md](../../k8s/README.md); each
level's bring-up is in §17.3–§17.7. The Level 2 core, as a reminder:

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

## 17.11 Gotchas

- **`-n thyme` on everything.** Without it you're querying `default` and
  everything looks empty. `kubectl config set-context --current --namespace=thyme`
  once, and stop typing it.
- **Deleting a StatefulSet keeps its PVC** (§17.9). This is the single most
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

## 17.12 Future enhancements

All six levels are built (§17.3–§17.8). What's left is polish:

- **An init container for the backend** that blocks on `pg_isready` before the app
  starts, so the boot-time `CrashLoopBackOff` (§17.5) never happens — the same fix
  [Ch 16 §16.6](13-containerization.md#166-gotchas) wants for multi-replica boots.
- **Pull from GHCR instead of `kind load`** — use the published images with an
  `imagePullSecret`, to exercise a real registry pull rather than the offline
  shortcut.

Later, if the curiosity holds: an HPA (to watch autoscaling react), resource
tuning under load, a backup `CronJob` for the PVC, and a `kustomize` overlay as a
counterpoint to Helm.
