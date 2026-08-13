# Thyme on Kubernetes — local learning sandbox

> **Not production.** This runs Thyme (frontend + backend + database) on a local
> [`kind`](https://kind.sigs.k8s.io/) cluster purely to learn Kubernetes. Real
> deploys stay on Vercel / Render / Neon. See `docs/DECISIONS.md` (2026-08-07)
> for the reasoning. Tear the whole thing down anytime with
> `kind delete cluster --name thyme`.

## The plan (raw manifests first, then Helm)

Manifests are written by hand so the primitives are understood *before* the
Helm templating layer is added on top.

| Level | Tier | Objects |
|------|------|---------|
| 1 | Cluster | `kind create cluster --name thyme` |
| 2 | **Database** | `database/` — Secret, headless Service, StatefulSet (+PVC) |
| 3 | **Backend** | Deployment, Service, Config/Secret (migrations on boot) |
| 4 | **Frontend** | Deployment, Service |
| 5 | **Ingress** | `thyme.local` → frontend, `/api` → backend (solves the build-time `NEXT_PUBLIC_API_URL` gotcha) |
| 6 | **Helm** | the same three tiers as a chart |

## Images

Built locally and loaded straight into the cluster (no registry auth):

```
kind load docker-image thyme-backend:dev  --name thyme
kind load docker-image thyme-frontend:dev --name thyme
```

## Level 1 — cluster

```
kind create cluster --name thyme
kubectl get nodes            # thyme-control-plane  Ready
```

## Level 2 — database

```
kubectl apply -f namespace.yaml
kubectl apply -f database/
kubectl -n thyme rollout status statefulset/postgres
# verify:
kubectl -n thyme exec -it postgres-0 -- psql -U thyme -d thyme -c '\conninfo'
```
