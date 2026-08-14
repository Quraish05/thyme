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

## Level 3 — backend

```
docker build -t thyme-backend:dev ../backend
kind load docker-image thyme-backend:dev --name thyme
kubectl apply -f backend/
kubectl -n thyme rollout status deployment/backend
# verify (migrations ran on boot; 15 tables + vector extension created):
kubectl -n thyme exec deploy/backend -- \
  python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/api/v1/health').read())"
```

## Level 4 — frontend

The API URL is baked in at build time, so it must point at the Ingress host:

```
docker build --build-arg NEXT_PUBLIC_API_URL=http://thyme.local -t thyme-frontend:dev ../frontend
kind load docker-image thyme-frontend:dev --name thyme
kubectl apply -f frontend/
kubectl -n thyme rollout status deployment/frontend
```

## Level 5 — Ingress

The Ingress needs a cluster with host port-mappings — recreate it from
`kind-cluster.yaml`, reload the images, and re-apply everything:

```
kind delete cluster --name thyme
kind create cluster --name thyme --config kind-cluster.yaml
kind load docker-image thyme-backend:dev thyme-frontend:dev --name thyme

# ingress-nginx controller
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml
kubectl wait -n ingress-nginx --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller --timeout=150s

# the whole app
kubectl apply -f namespace.yaml
kubectl apply -f database/
kubectl apply -f backend/
kubectl apply -f frontend/
kubectl apply -f ingress.yaml

# point the hostname at localhost (once; needs sudo)
echo "127.0.0.1 thyme.local" | sudo tee -a /etc/hosts
```

Then open <http://thyme.local>. Verify without editing /etc/hosts:

```
curl --resolve thyme.local:80:127.0.0.1 http://thyme.local/api/v1/health   # {"status":"ok",...}
curl --resolve thyme.local:80:127.0.0.1 http://thyme.local/                # <title>Thyme</title>
```

## Level 6 — Helm

The whole app as one chart ([helm/thyme/](helm/thyme)). A single `helm install`
replaces the Level 1–5 `kubectl apply` sequence, and `helm/thyme/values.yaml` is
the one place to change images, replicas, the Ingress host, or DB credentials.

```
# needs the Level 5 cluster (kind-cluster.yaml) + ingress-nginx already installed
kind load docker-image thyme-backend:dev thyme-frontend:dev --name thyme
helm install thyme helm/thyme --namespace thyme --create-namespace

helm -n thyme list                   # thyme   deployed   thyme-0.1.0
helm upgrade thyme helm/thyme        # apply changes to values/templates
helm uninstall thyme                 # remove every object in the release at once
```

Verify the same way as Level 5 (`curl --resolve thyme.local:80:127.0.0.1 …`).

## Tear down

```
kind delete cluster --name thyme     # cluster, volumes, data — all of it, free
```
