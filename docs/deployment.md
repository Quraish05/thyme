# Deployment

How **Thyme** is deployed so it's reachable from any device (including your
phone). Three services, all on free tiers:

| Piece | Host | Notes |
|-------|------|-------|
| Frontend (Next.js) | **Vercel** | Free hobby tier, HTTPS, serves the push service worker. |
| Backend (FastAPI) | **Render** | Free web service, built from `backend/Dockerfile`. Sleeps after ~15 min idle (cold start ~30–60s). |
| Database (Postgres) | **Neon** | Free *persistent* serverless Postgres, with `pgvector` for the journal RAG index. |

Two things sit alongside the deploy but are **not** part of it:

- **CI** — GitHub Actions gates every PR (`ruff` + `pytest`, `eslint` + `tsc` +
  `next build`) and publishes container images to GHCR after merge.
  See [handbook Ch 15](handbook/12-ci-pipelines.md).
- **The Kubernetes sandbox** in [`k8s/`](../k8s/) — a local, throwaway `kind`
  cluster for learning. It never touches production.
  See [handbook Ch 17](handbook/14-kubernetes-sandbox.md).

Backend packaging lives in `backend/`: [`Dockerfile`](../backend/Dockerfile),
[`entrypoint.sh`](../backend/entrypoint.sh),
[`.dockerignore`](../backend/.dockerignore) — walked through in
[handbook Ch 16](handbook/13-containerization.md). The Render service is described
by [`render.yaml`](../render.yaml) at the repo root.

> **Free-tier trade-offs.** The backend sleeps when idle, so the first request
> after a quiet spell is slow, and **no background loop runs while it's asleep** —
> which affects both reminder push and the job runner (see
> [§Known gaps](#known-gaps-worth-fixing)). Upgrading the Render service to a paid
> plan is the single change that fixes both.

---

## One-time prerequisites

- Accounts: [Neon](https://neon.tech), [Render](https://render.com),
  [Vercel](https://vercel.com) — all can sign in with GitHub.
- A Google Cloud project if you want Google Sign-In (Step 3).
- Generate a production JWT secret (paste it as an env var in Step 2):
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

Everything deploys straight from the GitHub repo — no CLI required.

---

## Step 1 — Database (Neon)

1. Create a Neon project (pick a region near Render's, e.g. Singapore).
2. Copy the **connection string** — looks like
   `postgresql://user:pass@ep-xxx.<region>.aws.neon.tech/dbname?sslmode=require`.
   Paste it as-is; the app normalizes the scheme and SSL options for asyncpg
   automatically (`config._normalize_database_url`).
3. Nothing else to do: `alembic upgrade head` runs on every backend boot and
   creates the `vector` extension itself.

---

## Step 2 — Backend (Render)

Render reads [`render.yaml`](../render.yaml) as a **Blueprint**:

1. Render dashboard → **New → Blueprint** → connect this GitHub repo.
2. Render detects `render.yaml` and proposes the `life-tracker-api` web service
   (Docker, `backend/` root, free plan). Approve it.
3. Fill in the environment variables it marks as required (the `sync: false`
   secrets — never committed):
   - `DATABASE_URL` — the Neon string from Step 1
   - `SECRET_KEY` — the token from prerequisites
   - `ANTHROPIC_API_KEY` — your key (or set `AI_PROVIDER=gemini` + `GEMINI_API_KEY`)
   - `SUPERADMIN_EMAIL` — the email you'll register with (unlimited AI)
   - `GOOGLE_CLIENT_ID` — from Step 3 (leave blank to disable Google Sign-In)
   - `CORS_ORIGINS` — a placeholder like `["https://example.vercel.app"]` for
     now; fixed in Step 5
4. **Also set these two** — they aren't in `render.yaml` yet and their defaults
   are wrong for production (see [Known gaps](#known-gaps-worth-fixing)):
   - `ENVIRONMENT=production` — switches logging to JSON and **removes the
     dev-only `/auth/dev/reset-ai-quota` route**
   - `JOBS_WORKER_ENABLED=true` — without it nothing runs queued jobs, so the
     weekly recap never generates
5. Deploy. On boot, [`entrypoint.sh`](../backend/entrypoint.sh) runs
   `alembic upgrade head` against Neon, then starts uvicorn.
6. Note the URL: `https://<service>.onrender.com`. Check both probes:
   ```bash
   curl https://<service>.onrender.com/api/v1/health        # process alive
   curl https://<service>.onrender.com/api/v1/health/ready  # database reachable
   ```

---

## Step 3 — Google Sign-In (optional, do it before Step 4)

Full detail in [handbook Ch 1](handbook/10-auth-and-google-sso.md); the deploy
checklist is short:

1. Google Cloud Console → **APIs & Services → Credentials → Create credentials →
   OAuth client ID → Web application**.
2. **Authorized JavaScript origins** — add every origin that renders the button:
   `http://localhost:3000` and `https://<project>.vercel.app`. (No redirect URIs;
   we use the ID-token flow, not the code flow.)
3. Copy the client ID (it's public, not a secret) and set it in **both** places
   with the **same value**:
   - Render: `GOOGLE_CLIENT_ID`
   - Vercel: `NEXT_PUBLIC_GOOGLE_CLIENT_ID`

A mismatch between the two fails the token's `aud` check, so every sign-in 401s
while the button itself looks fine. Blank on both sides is a clean disable: the
endpoint returns 503 and the button renders disabled.

---

## Step 4 — Frontend (Vercel)

1. Vercel → **Add New → Project** → import the repo.
2. **Set the Root Directory to `frontend`** (the app isn't at the repo root).
   Framework auto-detects as **Next.js**.
3. Environment variables — all `NEXT_PUBLIC_*`, all **baked in at build time**, so
   changing one requires a **redeploy**, not a restart
   ([why](handbook/13-containerization.md#164-the-tricky-part--next_public_-is-baked-in-at-build-time)):
   - `NEXT_PUBLIC_API_URL = https://<service>.onrender.com`
   - `NEXT_PUBLIC_GOOGLE_CLIENT_ID = <the id from Step 3>`
   - (Later, for push: `NEXT_PUBLIC_VAPID_PUBLIC_KEY = …`)
4. Deploy. Note the URL: `https://<project>.vercel.app`.

> `output: "standalone"` in `next.config.ts` only shapes the Docker build
> ([Ch 16](handbook/13-containerization.md)) — Vercel ignores it, so it's safe.

---

## Step 5 — Connect them (CORS)

The backend only accepts browser requests from origins in `CORS_ORIGINS`. In the
Render dashboard → your service → **Environment**, set:

```
CORS_ORIGINS = ["https://<project>.vercel.app"]
```

(A JSON array — quotes and brackets matter.) Saving triggers a redeploy. Open the
Vercel URL, register or sign in with Google, and you're live.

---

## Step 6 — Make yourself the superadmin

`SUPERADMIN_EMAIL` is promoted to superadmin on every boot. So:

1. Register (or Google sign-in) on the deployed site using that exact email.
2. Restart the service (Render → **Manual Deploy → Restart service**, or just
   redeploy) so the bootstrap runs. That account now has unlimited AI; everyone
   else gets the free quota (`AI_FREE_LIMIT`, default 5).
   *Verified locally: after a restart the account comes back as
   `role: superadmin`, `ai_remaining: null`.*

---

## Environment variable reference

Where each value has to live, and when it takes effect:

| Variable | Set on | Applies at | Notes |
|---|---|---|---|
| `DATABASE_URL` | Render | runtime | Neon string, pasted as-is |
| `SECRET_KEY` | Render | runtime | changing it invalidates every session |
| `CORS_ORIGINS` | Render | runtime | JSON array of exact origins |
| `ENVIRONMENT` | Render | runtime | `production` → JSON logs, no dev routes |
| `LOG_LEVEL` | Render | runtime | default `INFO` |
| `AI_PROVIDER` + key | Render | runtime | `anthropic` \| `gemini`; missing key → 503 |
| `AI_FREE_LIMIT` | Render | runtime | per-user lifetime AI calls |
| `SUPERADMIN_EMAIL` | Render | runtime | promoted on boot |
| `GOOGLE_CLIENT_ID` | Render | runtime | must equal the frontend's value |
| `JOBS_WORKER_ENABLED` | Render | runtime | `true` to run queued/scheduled jobs |
| `PUSH_DISPATCH_ENABLED` + VAPID keys | Render | runtime | needs an always-on plan |
| `NEXT_PUBLIC_API_URL` | Vercel | **build** | redeploy to change |
| `NEXT_PUBLIC_GOOGLE_CLIENT_ID` | Vercel | **build** | public by definition |
| `NEXT_PUBLIC_VAPID_PUBLIC_KEY` | Vercel | **build** | public half of the keypair |

The full backend surface, with comments, is
[`backend/.env.example`](../backend/.env.example).

---

## Container images (GHCR)

Every push to `master` that touches `backend/**` or `frontend/**` publishes two
images ([Ch 15 §15.5](handbook/12-ci-pipelines.md#155-publish-images--one-workflow-two-images)):

```
ghcr.io/quraish05/thyme-backend:latest    ghcr.io/quraish05/thyme-backend:sha-<short>
ghcr.io/quraish05/thyme-frontend:latest   ghcr.io/quraish05/thyme-frontend:sha-<short>
```

**Nothing in production consumes them yet** — Render builds its own image from
`backend/`, and Vercel builds from source. They exist as real artifacts for the
[K8s sandbox](handbook/14-kubernetes-sandbox.md) and as the groundwork for
deploying the *tested* artifact rather than rebuilding from source. They are
`linux/amd64` only, so on Apple Silicon prefer a local build.

---

## Using it on your phone

- Open the Vercel URL in mobile Safari/Chrome and **Add to Home Screen** — it
  behaves like an app (the service worker ships in `frontend/public/sw.js`).
- The first load after the backend has been idle takes ~30–60s (free-tier cold
  start); it's snappy after that until it goes idle again.

---

## Routine redeploys

Both services auto-deploy on push to the connected branch, and CI runs
independently on the same push:

- **Backend:** Render rebuilds the image; each deploy re-runs
  `alembic upgrade head` via the entrypoint.
- **Frontend:** Vercel rebuilds automatically — which re-bakes every
  `NEXT_PUBLIC_*` value.
- **CI:** `Backend CI` / `Frontend CI` gate the PR (~1 min each);
  `Publish images` runs after merge (~16 min).

> ⚠️ `master` currently has **no branch protection**, so a red CI run does not
> block a merge — and Render/Vercel will happily deploy it. Requiring both checks
> is the one-setting fix ([Ch 15 §15.7](handbook/12-ci-pipelines.md#157-gotchas)).

Rolling back: Render and Vercel both keep previous deploys and can redeploy them
from the dashboard. A migration is *not* rolled back by that — check whether the
older code tolerates the newer schema before rolling back.

---

## Known gaps worth fixing

Found by reading [`render.yaml`](../render.yaml) against
[`config.py`](../backend/app/core/config.py) — both are **defaults**, so if you've
already set these in the Render dashboard, you're fine. Verify in the dashboard
before assuming.

1. **`ENVIRONMENT` isn't in `render.yaml`**, so it defaults to `development` in
   production. Two consequences: logs render as pretty console lines instead of
   JSON ([Ch 8](handbook/03-observability.md)), and the **dev-only
   `POST /auth/dev/reset-ai-quota` route is registered** — it's guarded by
   `settings.environment != "production"` and nothing else, so anyone with a valid
   token can reset their own AI quota. Fix: `ENVIRONMENT=production`.
2. **`JOBS_WORKER_ENABLED` isn't in `render.yaml`** and defaults to `false`, so no
   job worker runs. `POST /recap/weekly/refresh` still returns `202` and enqueues a
   row, but nothing ever claims it — the job stays `queued` forever and the client
   polls a status that never changes, while the Monday `weekly_recap_all` schedule
   never fires. Fix: `JOBS_WORKER_ENABLED=true` (and note that on the free tier the
   loop still stops whenever the service sleeps).
3. **Both fixes belong in `render.yaml`**, not just the dashboard, so a fresh
   Blueprint deploy gets them.

---

## Deferred follow-ups

- **Server-side reminder push** (fires when the app is closed): needs an
  always-on backend, so first upgrade the Render service off free. Then generate a
  VAPID keypair (`npx web-push generate-vapid-keys`), set `VAPID_PUBLIC_KEY` /
  `VAPID_PRIVATE_KEY` / `VAPID_SUBJECT` + `PUSH_DISPATCH_ENABLED=true` on the
  backend, and `NEXT_PUBLIC_VAPID_PUBLIC_KEY` on the frontend.
- **Deploy the published image** instead of rebuilding from source, so the tested
  artifact is the shipped artifact.
- **Branch protection** on `master` requiring both CI gates.
- **A post-deploy smoke test** against `/api/v1/health/ready`.
- **Email verification** (parked earlier): Gmail SMTP + gate AI behind a verified
  email.
