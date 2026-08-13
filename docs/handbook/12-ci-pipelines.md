# Chapter 15 — CI: three GitHub Actions workflows

**Last updated:** 2026-08-13 · **Status:** ✅ current with `.github/workflows/` (backend `4dca8a3`, frontend `e8c0d80`, images `f5bd87b`)

**Why we did this.** The deploy story was already automated — push to `master`,
Render rebuilds the API, Vercel rebuilds the frontend. That's *continuous
deployment* with nothing in front of it. `ruff`, `pytest`, `eslint` and `tsc` all
existed, and all ran only when someone remembered to run them, which meant a
broken commit went to production at exactly the speed of a working one.

So the gap was never CD. It was **CI**: something that runs the checks we already
had, automatically, before the auto-deploy fires. See
[DECISIONS.md → 2026-08-07](../DECISIONS.md) for the full ladder we reasoned
through (Q3).

**What we have now.** Three workflows with two distinct jobs to do:

| Workflow | Trigger | Role |
|---|---|---|
| [backend-ci.yml](../../.github/workflows/backend-ci.yml) | PR + push to `master`, `backend/**` | **Gate** — ruff + pytest against real Postgres |
| [frontend-ci.yml](../../.github/workflows/frontend-ci.yml) | PR + push to `master`, `frontend/**` | **Gate** — eslint + tsc + next build |
| [publish-images.yml](../../.github/workflows/publish-images.yml) | push to `master`, manual | **Producer** — build & push images to GHCR |

---

## 15.1 Mental model — gates and producers

> **A gate answers one question: would merging this break something we already
> know how to check?** It runs on the *proposed* code and its only output is
> pass/fail. It must be fast (ours are ~1 minute) or people learn to ignore it.

> **A producer turns source into an artifact.** `publish-images` doesn't judge
> anything; it builds the two container images and pushes them to a registry so
> something downstream can pull them by name. It runs *after* merge because
> there's no point publishing an artifact from code that isn't going in.

Where they sit relative to the deploys we already had:

```
                     ┌── Backend CI  (gate) ──┐
PR opened ──────────►┤                        ├──► human merges
                     └── Frontend CI (gate) ──┘
                                                      │
push to master ───────────────────────────────────────┤
                                                      ├──► Render redeploys API   (was already happening)
                                                      ├──► Vercel redeploys FE    (was already happening)
                                                      └──► Publish images → GHCR  (new: the artifact)
```

Note what CI here does **not** do: it never deploys. Render and Vercel watch the
branch themselves. Our workflows are advisory checks plus an image build — which
keeps them simple, and keeps a CI outage from being a deploy outage.

---

## 15.2 Anatomy of a workflow (reading `backend-ci.yml` line by line)

Everything below is generic GitHub Actions vocabulary, illustrated with our file.

### Triggers, narrowed by path

```yaml
on:
  pull_request:
    paths: ["backend/**", ".github/workflows/backend-ci.yml"]
  push:
    branches: [master]
    paths: ["backend/**", ".github/workflows/backend-ci.yml"]
```

Two triggers, both path-filtered. A frontend-only PR doesn't spin up Postgres and
run pytest — it isn't skipped-but-charged, it simply doesn't run. The workflow
file includes *itself* in the paths, so a change to the pipeline is tested by the
pipeline.

Why also run on `push` to `master` when the PR already passed? Because `master`
can move underneath a PR: two independently-green branches can merge into a
broken `master`. The post-merge run is what catches that.

### Concurrency: stop working on stale commits

```yaml
concurrency:
  group: backend-ci-${{ github.ref }}
  cancel-in-progress: true
```

One run per branch. Push three quick fixes and the first two runs are cancelled
mid-flight, because you only care about the newest commit. On a shared free-tier
minute budget this matters more than it looks.

### Permissions: least privilege by default

```yaml
permissions:
  contents: read
```

Every run gets an automatic `GITHUB_TOKEN`. Declaring `permissions` shrinks what
that token can do — a gate needs to read code and nothing else. (Compare
`publish-images.yml`, which adds exactly one capability: `packages: write`.)

### `defaults` beats repeating `cd`

```yaml
defaults:
  run:
    working-directory: backend
```

In a monorepo every `run` step would otherwise start with `cd backend`. Note this
applies to `run` steps only — `uses:` actions (like `setup-node`'s cache) take
paths from the repo root, which is why `frontend-ci.yml` spells out
`cache-dependency-path: frontend/package-lock.json`.

### Steps: check out, install, check

```yaml
- uses: actions/checkout@v4              # the runner starts with an empty disk
- uses: astral-sh/setup-uv@v5            # same tool the Dockerfile uses
  with: { enable-cache: true }
- run: uv python install 3.13            # match the image's interpreter
- run: uv sync --frozen --python 3.13    # exactly what uv.lock pins
- run: uv run ruff check .
- run: uv run pytest
```

Two ideas worth naming, because they're the whole reason CI is trustworthy:

- **Frozen installs.** `uv sync --frozen` (and `npm ci` on the frontend) install
  *exactly* the lockfile and fail if it's stale. Without that, CI could resolve
  different versions than your laptop and disagree with it for reasons no one can
  reproduce. It also means a lockfile you forgot to commit is a CI failure — which
  is the correct outcome.
- **Version parity with production.** Python 3.13 here because
  [backend/Dockerfile](../../backend/Dockerfile) is `python:3.13-slim`. CI passing
  on a different interpreter than the one that ships is a false negative waiting
  to happen.

---

## 15.3 The tricky part — a real Postgres inside CI

Our test suite can't run on SQLite or fakes. Full-text search is Postgres
`tsvector` ([Ch 9](04-full-text-search.md)) and the journal RAG index is
`pgvector` ([Ch 12](08-journal-rag.md)); both are database features, not
application code. So CI has to bring a real database.

GitHub Actions calls this a **service container** — a container started alongside
the job, with its ports mapped onto the runner's `localhost`:

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    env:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: life_tracker_test
    ports: ["5432:5432"]
    options: >-
      --health-cmd "pg_isready -U postgres"
      --health-interval 10s
      --health-timeout 5s
      --health-retries 5
```

Four details, each of which was a way to get this wrong:

1. **`pgvector/pgvector:pg16`, not `postgres:16`.** The test harness runs
   `CREATE EXTENSION vector`, and an extension can only be created if it's
   installed in the image. Plain `postgres` fails at collection time with a
   confusing error about a missing extension.
2. **Health checks gate the first step.** Postgres accepts TCP connections before
   it's ready to serve. Without `--health-cmd`, the job races the database's
   startup and fails intermittently — the worst kind of red build. `pg_isready`
   is the canonical probe (the same one the [K8s sandbox](14-kubernetes-sandbox.md)
   uses for its readiness probe).
3. **`POSTGRES_DB` must match `TEST_DATABASE_URL`.** Our
   [conftest.py](../../backend/tests/conftest.py) connects to an *existing*
   database and creates the extension plus tables inside it; it does not
   `CREATE DATABASE`. So the name in the service env and the name in the URL are
   one fact expressed twice, and they must agree.
4. **The URL scheme is `postgresql+asyncpg://`.** conftest builds an async engine
   directly, so a bare `postgres://` fails on the driver, not on the connection.

```yaml
env:
  TEST_DATABASE_URL: postgresql+asyncpg://postgres:postgres@localhost:5432/life_tracker_test
```

The suite needs no migrations: the schema is built from the ORM models
(`create_all`) once per session, and every test runs inside a transaction that's
rolled back — including its `commit()`s, via
`join_transaction_mode="create_savepoint"`. That's why a full pytest run in CI
takes about as long as it does locally.

**No API keys.** Every AI test mocks its provider (and
[test_auth_google.py](../../backend/tests/test_auth_google.py) monkeypatches the
Google verifier), so CI needs zero secrets. That's a deliberate property of how
those tests were written, and it's why a fork's PR can run the full suite.

---

## 15.4 Frontend CI — three checks that fail for different reasons

```yaml
- run: npm ci          # exact lockfile install
- run: npm run lint    # eslint
- run: npx tsc --noEmit  # types only; tsconfig sets noEmit
- run: npm run build   # next build — the real compile
```

`next build` is the expensive one and it's the point: lint and types can both
pass while the production build fails (a server/client boundary violation, a bad
import in a route, an env var read at module scope). Since Vercel deploys by
running the same build, this step is a genuine rehearsal of the deploy.

It builds with `NEXT_PUBLIC_*` unset, falling back to the app's own defaults. That
proves the build doesn't *require* secrets — and it's also why CI can't catch a
wrong API URL. [Ch 16](13-containerization.md) explains why those values are a
build-time concern in the first place.

---

## 15.5 `publish-images` — one workflow, two images

This is the workflow that turns "it runs on my laptop" into a named artifact
anyone (including a `kind` cluster) can pull.

```yaml
strategy:
  fail-fast: false
  matrix:
    include:
      - { name: backend,  context: backend,  image: ghcr.io/quraish05/thyme-backend }
      - { name: frontend, context: frontend, image: ghcr.io/quraish05/thyme-frontend }
```

A **matrix** runs the same steps once per entry, in parallel. `fail-fast: false`
keeps a frontend failure from cancelling the backend build — with fail-fast on,
one flaky build costs you both artifacts.

```yaml
permissions:
  contents: read
  packages: write        # the only extra power this workflow needs
```

```yaml
- uses: docker/login-action@v3
  with:
    registry: ghcr.io
    username: ${{ github.actor }}
    password: ${{ secrets.GITHUB_TOKEN }}
```

**No secret to manage.** GHCR accepts the run's automatic `GITHUB_TOKEN`, which
is why `packages: write` above is the entire authorization story. (Docker Hub
would have meant creating an account, a token, and a repo secret.)

```yaml
- id: meta
  uses: docker/metadata-action@v5
  with:
    images: ${{ matrix.image }}
    tags: |
      type=raw,value=latest,enable={{is_default_branch}}
      type=sha,format=short
```

Every image gets **two** tags: `latest` (moving) and `sha-38b5fa0` (immutable).
The immutable one is the one that matters — `latest` can't tell you what's
running, and can't be rolled back to. Deploy by SHA, use `latest` for
convenience.

```yaml
- uses: docker/build-push-action@v6
  with:
    context: ${{ matrix.context }}
    platforms: linux/amd64
    push: true
    tags: ${{ steps.meta.outputs.tags }}
    labels: ${{ steps.meta.outputs.labels }}
    cache-from: type=gha
    cache-to: type=gha,mode=max
```

- **`cache-from/to: type=gha`** persists Docker layer cache in GitHub's cache
  service between runs. Without it every run reinstalls the Python dependency
  tree from scratch — and ours includes `torch` for local embeddings, which is why
  this job takes ~16 minutes rather than ~1.
- **`platforms: linux/amd64` only** — see the gotcha below if you're on an Apple
  Silicon Mac.

---

## 15.6 How to run & inspect

```bash
gh run list --limit 10                 # recent runs, per workflow
gh run watch                           # follow the in-flight one
gh run view <id> --log-failed          # just the failing step's log
gh workflow run publish-images.yml     # manual trigger (workflow_dispatch)
```

Reproduce a gate locally — the commands are deliberately the same ones CI runs:

```bash
cd backend  && uv sync --frozen && uv run ruff check . && uv run pytest
cd frontend && npm ci && npm run lint && npx tsc --noEmit && npm run build
```

Pull a published image and check what's inside it:

```bash
docker pull ghcr.io/quraish05/thyme-backend:latest
docker run --rm ghcr.io/quraish05/thyme-backend:latest python -V
```

Current timings, for calibration: **Backend CI ~1m15s**, **Frontend CI ~55s**,
**Publish images ~16m30s** (both images, warm cache).

---

## 15.7 Gotchas

- **⚠️ `master` has no branch protection, so green is advisory.** Nothing
  currently *prevents* merging a red PR — the checks are visible, not enforcing.
  One setting closes this: Settings → Branches → protect `master` → require
  "Backend CI" and "Frontend CI". Worth doing, since it's the difference between
  CI being a gate and CI being a notification.
- **Path filters and required checks interact badly.** Once a check is *required*,
  a PR that never triggers it (frontend-only, say) can sit "expected — waiting".
  The fix is a `paths-ignore`-style skip job or GitHub's "require only if run"
  behaviour — worth knowing before turning protection on.
- **`linux/amd64` images under-perform on Apple Silicon.** These images are
  single-arch. On an M-series Mac they run through emulation — slow, and
  occasionally weird for native wheels. For the [K8s sandbox](14-kubernetes-sandbox.md)
  prefer a locally built `:dev` image (`kind load docker-image`), or add
  `linux/arm64` to `platforms` and accept a longer publish.
- **A stale lockfile is a red build, not a warning.** `--frozen` / `npm ci` fail
  when the lockfile doesn't match the manifest. Commit the lockfile with the
  dependency change.
- **Service containers are reachable at `localhost`, not by hostname.** Ports are
  mapped onto the runner. A `docker`-network-style hostname (`postgres:5432`)
  works only if the job itself runs in a container.
- **The database name lives in two places** (§15.3). Change one, change both.
- **Cancelled runs look like failures in some views.** `cancel-in-progress` means
  superseded runs show as cancelled — that's the feature working, not a flake.
- **Nothing scans for secrets yet.** `gitleaks` and Dependabot were the Tier-2
  items in the decision log and remain unimplemented; there are API keys and an
  OAuth client id in this project's environment surface.

---

## 15.8 Future enhancements

- **Branch protection** on `master` requiring both gates (§15.7) — the single
  highest-value change left in this chapter.
- **Dependabot** (one YAML) for dependency and security PRs; **gitleaks** as a
  third gate.
- **Multi-arch images** (`linux/amd64,linux/arm64`) once the sandbox actually
  pulls from GHCR on an ARM laptop.
- **Deploy from the artifact, not from source** — today Render rebuilds the image
  itself from `backend/`, so the thing CI published is not literally the thing
  that ships. Pointing Render at `ghcr.io/quraish05/thyme-backend:sha-…` would
  make the tested artifact and the deployed artifact the same bytes.
- **A smoke test after deploy** — poll `/api/v1/health/ready`
  ([Ch 8](03-observability.md)) and fail loudly if a deploy comes up unhealthy.
