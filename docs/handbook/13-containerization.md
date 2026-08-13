# Chapter 16 — Containerizing both apps

**Last updated:** 2026-08-13 · **Status:** ✅ current with `backend/Dockerfile` + `frontend/Dockerfile` (`45adb25`)

**Why we did this.** The backend has shipped as a container since it went to
Render — that was never optional, since Render's Docker runtime *is* the deploy
target. The frontend was a different story: Vercel builds Next.js from source and
needs no Dockerfile at all. We containerized it anyway, for two reasons that both
turn out to be about learning:

1. The [Kubernetes sandbox](14-kubernetes-sandbox.md) can only run what it can
   pull. No frontend image, no three-tier cluster.
2. Packaging Next.js *properly* forces you to confront where its configuration
   actually lives — and the answer (build time, in the browser's bundle) is the
   single most important gotcha in the whole deploy story. §16.4 is that story.

---

## 16.1 Mental model — an image is a filesystem plus a default command

> **A container image is a stack of read-only filesystem layers and a bit of
> metadata** (default command, env, exposed port, user). Each instruction in a
> Dockerfile that changes the filesystem produces a layer. `docker run` mounts
> that stack, adds a writable layer, and runs the command.

Two consequences drive every decision below:

> **Layer caching is positional.** Docker reuses a cached layer only if that
> instruction *and every instruction before it* are unchanged. So the order of a
> Dockerfile is a performance design: things that rarely change (dependency
> manifests) go near the top, things that change every commit (source code) go
> near the bottom. Get this backwards and every one-line edit reinstalls the world.

> **The build context is uploaded before anything runs.** `docker build backend/`
> tars up that directory and sends it to the daemon. `.dockerignore` is therefore
> both a speed knob and a security boundary — files it excludes cannot end up in
> the image, even by accident.

The two Dockerfiles take deliberately different shapes:

| | Backend | Frontend |
|---|---|---|
| Stages | 1 (single-stage) | 3 (`deps` → `builder` → `runner`) |
| Base | `python:3.13-slim` | `node:20-alpine` |
| Why that shape | Python runs from source; build tools are small | The build output (`.next/standalone`) is a *different, smaller thing* than the build inputs |
| Runs as | root | non-root (`nextjs`, uid 1001) |
| Listens on | `${PORT:-8080}` | `3000` |
| Entrypoint | `./entrypoint.sh` (migrate, then serve) | `node server.js` |

---

## 16.2 The backend image, line by line

[backend/Dockerfile](../../backend/Dockerfile):

```dockerfile
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
```

- **`-slim`** is Debian minus the parts a runtime doesn't need — a fraction of the
  full image, without the musl/glibc surprises of Alpine (which matter here,
  because some dependencies ship native wheels).
- **`PYTHONUNBUFFERED=1`** is the one people learn the hard way: without it Python
  buffers stdout when it isn't a TTY, so your logs appear late, out of order, or
  not at all when a container is killed. Every containerized Python app wants
  this ([Ch 8](03-observability.md) is pointless without it).
- **`PYTHONDONTWRITEBYTECODE=1`** — don't litter `.pyc` files into the writable
  layer at runtime…
- **…while `UV_COMPILE_BYTECODE=1`** *does* precompile bytecode at build time.
  Not a contradiction: compile once into an immutable layer, and never pay the
  compile cost on cold start.
- **`UV_LINK_MODE=copy`** — uv hardlinks packages into the venv by default, which
  fails across Docker layer boundaries; copying is the correct mode in an image.

```dockerfile
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
```

`COPY --from=<image>` lifts a single file out of another image — here the `uv`
binary, with no `pip install` bootstrap and no Python packaging in the final
layer. (This is the same mechanism as multi-stage `COPY --from=builder`, just
pointed at a published image instead of a local stage.)

```dockerfile
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY . .
RUN uv sync --frozen --no-dev && chmod +x entrypoint.sh
```

This is the cache-ordering idea made concrete, and it's the most transferable
pattern in the file:

1. Copy **only** the dependency manifests.
2. Install dependencies but **not the project itself** (`--no-install-project`)
   and **not dev extras** (`--no-dev`). This layer is expensive — it includes
   `torch` for local embeddings — and it is cached until `uv.lock` changes.
3. *Then* copy the source, and run `uv sync` again to install the project itself.
   That second sync is nearly instant.

Editing a route file rebuilds only steps 3+. Without the split, it would
reinstall the entire dependency tree — minutes per commit, and the reason
[CI's image job](12-ci-pipelines.md#155-publish-images--one-workflow-two-images)
leans so hard on layer caching.

```dockerfile
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
CMD ["./entrypoint.sh"]
```

Putting the venv's `bin` on `PATH` means `uvicorn` and `alembic` resolve directly
— no `uv run` wrapper process in production. `EXPOSE` is documentation (plus a
hint to tooling); it does not publish anything.

### The entrypoint: migrate, then `exec`

[entrypoint.sh](../../backend/entrypoint.sh):

```sh
set -e
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}"
```

Four decisions in five lines:

- **`set -e`** — if migrations fail, exit non-zero and let the platform restart
  the container. Serving against a schema you didn't get to is worse than being
  down.
- **Migrations run here** because Render's free tier has no pre-deploy hook.
  `alembic upgrade head` is idempotent, so a no-op boot is cheap.
- **`exec`** replaces the shell with uvicorn, making it **PID 1**. Without it, the
  shell is PID 1, `SIGTERM` goes to the shell, and uvicorn never gets the signal
  — so shutdown hangs until the platform's kill timeout, and the `lifespan`
  cleanup ([Ch 14](11-fastapi-request-journey.md), where the job worker and
  reminder loop are stopped) never runs.
- **`--host 0.0.0.0`, `${PORT:-8080}`** — binding to `127.0.0.1` inside a
  container means nothing outside can reach it, and reading `$PORT` is what makes
  the same image portable across Render, Railway, Fly and a K8s pod.

---

## 16.3 The frontend image: three stages, one small runner

[frontend/Dockerfile](../../frontend/Dockerfile). The unlock is one line in
[next.config.ts](../../frontend/next.config.ts):

```ts
output: "standalone",
```

That makes `next build` emit `.next/standalone` — a self-contained server
(`server.js`) bundled with only the `node_modules` it actually needs. So the final
image doesn't ship the dependency tree, the source, or the build toolchain.

```dockerfile
# --- deps ---
FROM node:20-alpine AS deps
COPY package.json package-lock.json ./
RUN npm ci                                  # cached until the lockfile changes

# --- builder ---
FROM node:20-alpine AS builder
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# --- runner ---
FROM node:20-alpine AS runner
RUN addgroup -g 1001 -S nodejs && adduser -u 1001 -S nextjs -G nodejs
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
CMD ["node", "server.js"]
```

Notes on the parts that bite:

- **Three `COPY`s, not one.** The standalone output deliberately excludes
  `public/` and `.next/static/`, and `server.js` expects to find them at those
  exact paths. Miss the `static` copy and you get a running app with no CSS or
  JS chunks — a blank page, no error.
- **`USER nextjs`** — the runtime never needs root. (The backend image still runs
  as root; see §16.6.)
- **`HOSTNAME=0.0.0.0`** in the runner's `ENV` is the Next.js equivalent of
  uvicorn's `--host`: the standalone server otherwise binds the loopback and is
  unreachable from outside the container.
- **`deps` as its own stage** exists purely so `npm ci` caches independently of
  source changes — the same cache-ordering trick as the backend's split sync.

---

## 16.4 The tricky part — `NEXT_PUBLIC_*` is baked in at build time

This is the detail that shapes how the frontend can be deployed anywhere, and
it's worth understanding precisely because it contradicts how server-side config
normally works.

**The rule:** a `NEXT_PUBLIC_*` variable is *inlined as a literal string into the
client JavaScript bundle when `next build` runs.* It is not read at startup. The
built image contains `"http://localhost:8000"` the way it contains any other
string constant.

**Why that's unavoidable:** the code that uses `NEXT_PUBLIC_API_URL` runs **in
the user's browser**. A browser has no access to the container's environment; the
value has to be *in the bundle it downloads*. So it must be decided at build time.

Hence the build-args in the builder stage:

```dockerfile
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ARG NEXT_PUBLIC_GOOGLE_CLIENT_ID=""
ARG NEXT_PUBLIC_VAPID_PUBLIC_KEY=""
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL ...
RUN npm run build
```

```bash
docker build frontend \
  --build-arg NEXT_PUBLIC_API_URL=https://api.example.com \
  --build-arg NEXT_PUBLIC_GOOGLE_CLIENT_ID=1234-abc.apps.googleusercontent.com \
  -t thyme-frontend:dev
```

Three consequences to internalise:

1. **`-e NEXT_PUBLIC_API_URL=…` at `docker run` does nothing.** The value is
   already compiled in. This is the number-one "my container ignores my env var"
   confusion, and it's not a bug.
2. **One image per API URL.** Strictly, the "build once, deploy anywhere" ideal
   breaks for these values — dev, staging and prod need different builds, or a
   different strategy.
3. **In-cluster DNS is useless here.** In Kubernetes it's tempting to set
   `NEXT_PUBLIC_API_URL=http://backend:8000`. That name only resolves *inside* the
   cluster, and the caller is a browser on your laptop. It will never resolve.
   The fix is to make the API reachable at the same origin the browser already
   uses — one host (`thyme.local`) with an Ingress routing `/` → frontend and
   `/api` → backend. That's exactly why Level 5 exists in the
   [sandbox curriculum](14-kubernetes-sandbox.md).

(A build-time `NEXT_PUBLIC_*` is also *public* by definition — it ships to every
visitor. The Google client id and VAPID public key are fine; nothing secret can
go here.)

---

## 16.5 How to run

Backend — note that config comes in at **run** time (the opposite of the frontend):

```bash
docker build -t thyme-backend:dev backend
docker run --rm -p 8000:8080 \
  -e DATABASE_URL='postgresql+asyncpg://user:pass@host:5432/thyme' \
  -e SECRET_KEY=dev-secret \
  -e GEMINI_API_KEY=… \
  thyme-backend:dev
curl localhost:8000/api/v1/health/ready
```

On macOS, reaching a Postgres running on the host from inside the container means
`host.docker.internal` rather than `localhost`.

Frontend:

```bash
docker build -t thyme-frontend:dev frontend \
  --build-arg NEXT_PUBLIC_API_URL=http://localhost:8000
docker run --rm -p 3000:3000 thyme-frontend:dev
```

Useful pokes at either image:

```bash
docker run --rm thyme-backend:dev python -V           # confirm the interpreter
docker run --rm -it thyme-backend:dev sh              # look around the filesystem
docker image ls | grep thyme                          # sizes
docker history thyme-backend:dev                      # which layer costs what
```

### `.dockerignore` does real work

Both files exclude `.env`/`.env.*` (secrets must never be baked into a layer —
and layers are extractable, so "it's not in the final stage" is not protection),
`.git/`, caches and `*.md`. The backend also excludes `tests/`, which is why you
can't run pytest inside the shipped image: the test suite is a build-time
artifact, not a runtime one.

> Note the asymmetry worth remembering: `.env` is excluded but `!.env.example` is
> kept, so the image documents its own configuration surface without carrying any
> real values.

---

## 16.6 Gotchas

- **Env vars land at different times per app.** Backend: runtime. Frontend
  `NEXT_PUBLIC_*`: build time (§16.4). Mixing these up costs an afternoon.
- **The backend container runs as root.** Unlike the frontend, there's no
  `USER` line. Fine for Render, wrong for a hardened cluster — see §16.7.
- **Migrations run on every boot**, from *every* replica. With one instance
  that's ideal; scale to several and they race on `alembic upgrade head` (Alembic
  takes a lock, so the usual outcome is a slow boot rather than corruption, but
  it's not a design that scales). A K8s `Job` or init container is the real fix.
- **Neither image declares a `HEALTHCHECK`.** Health is checked externally —
  Render via `healthCheckPath: /api/v1/health`, K8s via probes. If you run these
  with bare `docker run`, nothing is watching.
- **Published images are `linux/amd64` only** ([Ch 15](12-ci-pipelines.md)).
  On Apple Silicon prefer a local build.
- **`COPY . .` invalidates everything below it.** That's why it sits after the
  dependency install — and why adding a `COPY` above it silently destroys the
  cache.
- **Missing `.next/static` = a blank page, not an error** (§16.3).
- **`localhost` inside a container is the container.** Applies to binding
  (`--host 0.0.0.0`, `HOSTNAME=0.0.0.0`) and to connecting (`host.docker.internal`).
- **No `docker-compose.yml` in this repo.** Local dev runs the apps natively
  against a local Postgres; the containers exist for deploy targets and the K8s
  sandbox. One-command local orchestration is a future nicety, not a current path.

---

## 16.7 Future enhancements

- **Multi-stage the backend** — build the venv in a stage that has `uv` and any
  build toolchain, copy only `/app/.venv` and the app into a clean
  `python:3.13-slim`. Smaller image, no build tools in the runtime.
- **Add a non-root `USER`** to the backend image (and a read-only root filesystem
  where possible).
- **A migration `Job`/init container** instead of migrating in the entrypoint, so
  N replicas don't each try (§16.6).
- **Multi-arch builds** so ARM laptops and the sandbox can pull the same tags CI
  publishes.
- **Same-origin frontend config** — proxy `/api` through the frontend (Next.js
  rewrites or an Ingress), so `NEXT_PUBLIC_API_URL` becomes a relative path and
  the image stops being environment-specific. This is the clean fix to §16.4 and
  makes one image genuinely deployable anywhere.
