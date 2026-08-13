# Chapter 14 — Inside a FastAPI request: three journeys

**Last updated:** 2026-08-12 · **Status:** ✅ current with FastAPI 0.139.2

**Why this chapter exists.** Every other chapter explains a *feature*. This one
explains the *machinery* underneath all of them: what actually happens between
"the browser called `fetch`" and "your `async def` body runs", and what happens
on the way back out. It's written for someone who can read Python but has never
built a FastAPI app — so it's deliberately slower and more literal than the rest
of the book.

We trace three real endpoints, chosen because they fail and behave differently:

| | Endpoint | Shape |
|---|---|---|
| **1** | `POST /api/v1/notes` | The plain path — validate, write, commit, respond |
| **2** | `POST /api/v1/recap/weekly/refresh` | Accept the work, do it later, respond `202` |
| **3** | `POST /api/v1/chat` | Stream — respond *first*, do the work while streaming |

Read journey 1 closely; 2 and 3 are mostly "what changes, and why".

---

## 14.1 Mental model — an onion around one function

> **FastAPI is not a server.** It's an ASGI *application*: a callable that
> receives `(scope, receive, send)`. `uvicorn` is the server that speaks HTTP and
> calls it. FastAPI itself is routing + dependency injection + validation layered
> on top of Starlette (ASGI plumbing) and Pydantic (validation).

> **A route handler is a leaf, not an entry point.** By the time your function
> body runs, five or six layers have already done work — and each of them can
> answer the request *without ever calling you* (401 from the auth scheme, 422
> from validation, 404 from the router). Most "why didn't my handler run?"
> confusion is really "which layer answered instead?".

The onion, outermost first, with our actual code:

```
uvicorn (HTTP, ASGI)
└── FastAPI app                                     app/main.py: create_app()
    └── RequestContextMiddleware  ← outermost mw    app/api/middleware.py
        └── CORSMiddleware                          added first, so it runs inside
            └── Router: match method + path         app/api/router.py
                └── per-request AsyncExitStack      (FastAPI, for `yield` deps)
                    └── Dependencies                app/api/deps.py
                        │   HTTPBearer → get_current_user → get_db
                        └── Body validation         app/schemas/*.py
                            └── ★ your handler      app/api/routes/*.py
                            ── response_model serialization ──►
```

**Middleware order is inverted from the order you add it.** In
[main.py](../../backend/app/main.py) CORS is added first and `RequestContextMiddleware`
second, and the comment says why: the last one added wraps everything, so a
`request_id` exists before any other layer can log.

---

## 14.2 How a URL finds a function

Three separate prefixes concatenate, which is why grepping for the literal path
string in this repo finds nothing:

```python
app.include_router(api_router, prefix=settings.api_v1_prefix)  # "/api/v1"   main.py
api_router.include_router(notes.router)                        #             router.py
router = APIRouter(prefix="/notes", tags=["notes"])            # "/notes"    routes/notes.py
@router.post("")                                               # ""
#  →  POST /api/v1/notes
```

Routes are matched **in registration order**, first match wins. That has a real
consequence in [routes/notes.py](../../backend/app/api/routes/notes.py):

```python
@router.get("/search")      # declared FIRST
@router.get("/{note_id}")   # declared SECOND
```

Reverse those two and `GET /notes/search` matches `/{note_id}`, FastAPI tries
`int("search")`, and the user gets a baffling `422` instead of search results.
**Static path segments must be declared before the parameterised ones that could
swallow them.**

---

## 14.3 Journey 1 — `POST /api/v1/notes` (the plain path)

The client sends:

```http
POST /api/v1/notes HTTP/1.1
Authorization: Bearer eyJhbGciOi...
Content-Type: application/json

{"kind": "note", "title": "Groceries", "body_md": "milk, thyme"}
```

The handler is five lines
([notes.py:68](../../backend/app/api/routes/notes.py#L68)):

```python
@router.post("", response_model=NoteRead, status_code=status.HTTP_201_CREATED, ...)
async def create_note(payload: NoteCreate, current_user: CurrentUser, db: DbSession) -> Note:
    note = Note(user_id=current_user.id, **payload.model_dump())
    db.add(note)
    await db.commit()
    await db.refresh(note)
    await reindex_note_safe(db, note)
    return note
```

Here is everything that happens around those five lines.

```mermaid
sequenceDiagram
  autonumber
  participant C as Client
  participant MW as RequestContextMiddleware
  participant R as Router
  participant D as Dependencies
  participant H as create_note
  participant DB as Postgres

  C->>MW: POST /api/v1/notes
  MW->>MW: mint request_id, bind log context, start timer
  MW->>R: call_next
  R->>R: match POST + /api/v1/notes
  R->>D: open per-request AsyncExitStack, solve deps
  D->>D: HTTPBearer: read Authorization header (401 if absent)
  D->>D: get_db: open AsyncSession (suspended at `yield`)
  D->>DB: get_current_user: SELECT users WHERE id = sub
  D->>D: validate body into NoteCreate (422 if bad)
  D->>H: call handler(payload, current_user, db)
  H->>DB: INSERT notes ... ; COMMIT
  H->>DB: SELECT the row back (refresh: server defaults)
  H->>DB: reindex_note_safe (journal entries only)
  H-->>R: return the ORM Note object
  R->>R: serialize through NoteRead, status 201
  R->>D: close the AsyncExitStack → session closed
  R-->>MW: Response
  MW->>MW: log request_completed (status, duration_ms)
  MW-->>C: 201 + X-Request-ID
```

### The three parameters, and where each comes from

FastAPI decides what to inject **from the type annotation alone** — there is no
config listing which parameter is the body:

| Parameter | Annotation | Resolved as |
|---|---|---|
| `payload` | `NoteCreate` (a `BaseModel`) | the **JSON body**, parsed and validated |
| `current_user` | `CurrentUser` = `Annotated[User, Depends(get_current_user)]` | a **dependency** |
| `db` | `DbSession` = `Annotated[AsyncSession, Depends(get_db)]` | a **dependency** |

A Pydantic model annotation means body. A scalar (`note_id: int`) matching a path
placeholder means path param; a scalar that doesn't means query param. Anything
wrapped in `Depends` is a dependency. That's the whole rule set.

### The dependency graph, and the free lunch inside it

`get_current_user` *also* asks for `DbSession`, so the graph is:

```
create_note ──► get_current_user ──► bearer_scheme (HTTPBearer)
      │                   └──────────► get_db
      └──────────────────────────────► get_db
```

`get_db` appears twice but runs **once**: FastAPI caches each dependency's result
per request (`use_cache=True` by default), keyed by the callable. So the user
lookup and the `INSERT` share one session and one transaction. That's not a
detail — it's the reason `current_user` is a live ORM object the handler can
mutate and commit (exactly what
[ai_quota.py](../../backend/app/api/ai_quota.py)'s `record_ai_usage` does).

### `get_db` is a context manager pretending to be a function

```python
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session
```

Everything before `yield` is setup, everything after (here, `__aexit__` of the
`async with`) is teardown. FastAPI registers that teardown on a per-request
`AsyncExitStack` — remember this; journey 3 turns on exactly *when* the stack
closes.

Two settings in [session.py](../../backend/app/db/session.py) shape every handler
in the app:

- **One session per request**, not per app. Sessions are cheap; connections come
  from the engine's pool. A shared global session would serialise every request
  and leak state between users.
- **`expire_on_commit=False`.** By default SQLAlchemy marks every attribute stale
  after `commit()`, so touching `note.id` would fire a fresh `SELECT` — and in
  async code that lazy load raises `MissingGreenlet` instead of quietly working.
  Turning it off is what lets us `return note` (or `_token_for(user)` in
  [Ch 1](10-auth-and-google-sso.md)) straight after a commit.

### Validation happens *before* your code, and it answers for you

If the body is malformed — missing `kind`, a string where a date belongs, a
title over the max length — the handler is never called. FastAPI returns **422**
with a per-field error list, generated from
[schemas/note.py](../../backend/app/schemas/note.py). No `if not title: return
error` code anywhere.

Worth internalising the split:

- **422** — the request didn't match the *schema*. FastAPI's job, automatic.
- **404 / 409 / 429** — the request was well-formed but the *world* disagrees
  (no such note, email taken, quota spent). Your job, via `raise HTTPException`.
- **500** — you didn't handle something. The middleware logs
  `request_failed` with the traceback and re-raises.

Notice the handlers raise `HTTPException` rather than returning error responses.
Raising unwinds out of nested helpers — `_get_owned_note()` is three call frames
deep and still ends the request cleanly.

### Ownership is checked in code, not by the router

```python
note = Note(user_id=current_user.id, **payload.model_dump())
```

The client cannot set `user_id`, because `NoteCreate` has no such field — an
attacker sending `{"user_id": 99}` has it silently dropped. On reads, the same
discipline appears as a filter (`.where(Note.user_id == current_user.id)`) or as
`_get_owned_note()`, which returns **404, not 403**, for someone else's row —
telling a stranger "that id exists but isn't yours" is itself a leak.

### The way out: `response_model`

The handler returns a SQLAlchemy `Note`. The client receives JSON. `NoteRead`
does that conversion (via `ConfigDict(from_attributes=True)`), and it is also a
**filter** — anything not declared on `NoteRead` cannot escape, which is how
`hashed_password` can sit on the `User` model and never appear in a response.

It also feeds `/docs`: the OpenAPI schema is generated from these annotations,
which is why the `responses={**UNAUTHORIZED_RESPONSE}` spread exists on nearly
every route — documenting the 401 that the dependency, not the handler, produces.

### Teardown order

Handler returns → response serialized → **then** the exit stack closes the
session → then the middleware logs `request_completed` with the duration and
stamps `X-Request-ID`. The access log line is emitted last, so its `duration_ms`
includes the database cleanup.

---

## 14.4 Journey 2 — `POST /recap/weekly/refresh` (202: promise now, work later)

Recomputing a week-in-review scans several tables. Doing it inline would work…
until it doesn't: the user waits, a mobile connection times out mid-way, and the
retry starts from scratch. So this endpoint doesn't do the work
([recap.py](../../backend/app/api/routes/recap.py)):

```python
@router.post("/weekly/refresh", response_model=RecapJobStatus,
             status_code=status.HTTP_202_ACCEPTED, ...)
async def refresh_weekly_recap(current_user: CurrentUser, db: DbSession) -> RecapJobStatus:
    job_id = await enqueue(db, kind=_JOB_KIND, payload={"user_id": current_user.id})
    return RecapJobStatus(job_id=job_id, status="queued")
```

**`202 Accepted` is the honest status code**: not "here's your recap" (200), but
"I've written down that you want one". The *only* thing the handler does is
`INSERT` a row into `jobs` and commit.

```mermaid
sequenceDiagram
  participant C as Client
  participant API as refresh_weekly_recap
  participant DB as jobs table
  participant W as Worker loop (same process, own task)

  C->>API: POST /recap/weekly/refresh
  API->>DB: INSERT jobs(kind='weekly_recap', payload={user_id}) ; COMMIT
  API->>W: signal_job_change()  (asyncio.Event)
  API-->>C: 202 { job_id, status: "queued" }
  Note over API,C: request is over; the work hasn't started
  W->>DB: SELECT ... WHERE status='queued' AND run_at<=now() FOR UPDATE SKIP LOCKED
  W->>DB: status='running' ; COMMIT   (lock released before the handler runs)
  W->>W: handle_weekly_recap(payload) — opens its OWN session
  W->>DB: upsert weekly_recaps ; status='done' ; COMMIT
  C->>API: GET /recap/weekly/status/{job_id}  (polling)
  API-->>C: { status: "done" } → client refetches GET /recap/weekly
```

Three things a learner should take from this shape:

**1 · Commit, then signal — never the reverse.** `enqueue()` commits the row and
*then* sets the `asyncio.Event` that wakes the sleeping worker. Signalling first
would let the worker query before the row is visible and go back to sleep. (The
loop also clears the event *before* querying, so a job enqueued during the query
re-sets it and forces another pass. See
[queue.py](../../backend/app/services/jobs/queue.py).)

**2 · Background work needs its own session.** The request's session dies with
the request. `handle_weekly_recap` opens `async_session_factory()` itself. Trying
to hand a request-scoped session to something that outlives the request is one of
the most common async-Python bugs — the request ends, the session closes, and the
"background" code fails at its first query.

**3 · Ownership without a foreign key.** The job's owner lives in
`payload["user_id"]` (JSONB), so the status endpoint checks it manually and
returns **404** for a mismatch:

```python
if job is None or job.kind != _JOB_KIND or job.payload.get("user_id") != current_user.id:
    raise HTTPException(404, JOB_NOT_FOUND)
```

Job ids are sequential integers, so without that check anyone could enumerate
other people's jobs.

> **Aside — where does the worker even run?** It's an `asyncio.Task` started in
> the app's `lifespan` in [main.py](../../backend/app/main.py), inside the same
> process as the API. No Celery, no Redis, no second deployment: one process,
> one event loop, cooperatively shared. That works precisely because our handlers
> `await` on I/O — a CPU-bound handler, or a blocking `time.sleep`, would stall
> the API too. The full treatment is Ch 5 (planned).

---

## 14.5 Journey 3 — `POST /chat` (streaming: the response starts before the work)

The chat assistant may run several model rounds and call tools. Waiting for all
of it before sending a byte would feel broken, so the response is
Server-Sent Events ([chat.py](../../backend/app/api/routes/chat.py)):

```python
async def chat(payload: ChatRequest, current_user: CurrentUser, db: DbSession) -> StreamingResponse:
    if not chat_configured():          raise HTTPException(503, ...)
    if payload.messages[-1].role != "user": raise HTTPException(422, ...)
    enforce_ai_quota(current_user)      # 429 if the free pool is spent

    async def event_stream():
        async for frame in stream_chat(payload.messages, user=current_user, db=db,
                                       timezone=payload.timezone, on_complete=charge):
            yield frame

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
```

**The handler returns in microseconds.** It hands back a `StreamingResponse`
wrapping an async generator that hasn't run yet. Starlette then iterates it,
sending each yielded chunk down the open connection.

### The rule this shape forces on you

An HTTP status code is sent in the **first** bytes of the response. Once
streaming begins, `raise HTTPException(502)` is far too late — the client already
has `200 OK` and an open body. In practice:

- **Everything that can fail cheaply must be checked *before* returning the
  response.** That's exactly why those three guards (503 / 422 / 429) sit above
  the generator, in that order.
- **Failures after that must be *content*, not status.** `stream_chat` catches
  provider errors and yields an `{"type": "error"}` SSE frame, then returns — so
  the stream always ends cleanly and the UI can render the failure. Its docstring
  says so explicitly.

### Does the DB session survive long enough?

The generator uses `db` after the handler has returned: tools read and write the
user's data, and `charge()` commits the quota increment. So: is the session from
`get_db` still open?

In **FastAPI 0.139.2, yes** — and you can see it in the vendored request handler
(`.venv/.../fastapi/routing.py`, `request_response`):

```python
async with AsyncExitStack() as request_stack:          # yield-dependency teardown
    scope["fastapi_inner_astack"] = request_stack
    async with AsyncExitStack() as function_stack:
        response = await f(request)                    # ← handler returns here
    await response(scope, receive, send)               # ← body streamed here
# ← get_db's teardown runs only now
```

The stack that holds `get_db`'s cleanup wraps **both** the handler call *and* the
sending of the body. Sending a `StreamingResponse`'s body means draining the
generator, so the session is closed only after the last frame.

This ordering has genuinely moved between FastAPI releases, so treat it as a
property of the pinned version, not a law. If you need certainty regardless of
version, open a session inside the generator (`async_session_factory()`) the way
the job handlers do.

### Charging on the success path only

Quota is *enforced* up front but *recorded* by a callback the generator invokes
just before the `done` frame:

```python
async def charge() -> None:
    await record_ai_usage(current_user, db)
```

So a turn that dies mid-stream costs the user nothing — the same
"charge after the work succeeds" rule the other AI features follow
([Ch 10](05-ai-nutrition-estimation.md), [Ch 11](06-ai-chat-tools.md)).

`X-Accel-Buffering: no` is the other detail: a reverse proxy that buffers the
response would hold frames back and destroy the streaming effect.

---

## 14.6 The three shapes, side by side

| | 1 · `POST /notes` | 2 · `POST /recap/.../refresh` | 3 · `POST /chat` |
|---|---|---|---|
| Status | `201` | `202` | `200`, immediately |
| When the work happens | inside the handler | after the response, in the worker | during the response |
| Client waits for the result | yes | no — it polls | it watches it arrive |
| DB session | request-scoped, closed at teardown | request session for the `INSERT`; worker opens its own | request-scoped, alive until the last frame |
| Late failure reported as | HTTP status | job row → `failed` + `error` | an SSE `error` frame |
| Retry story | client resubmits | `attempts` / `max_attempts`, backoff | user sends the turn again |

The decision rule that generated the table: **can the client usefully wait?** Yes
and it's fast → journey 1. No → journey 2. It can wait, but only if it sees
progress → journey 3.

---

## 14.7 How to run & explore

```bash
cd backend && uv run fastapi dev app/main.py
```

- **`http://localhost:8000/docs`** — every route, its schemas, its documented
  error responses, with a working "Try it out". This is generated from the type
  annotations discussed above; it's the fastest way to see the effect of a schema
  change.
- **Trace one request end to end.** Send your own correlation id and grep for it:
  ```bash
  curl -s -H 'X-Request-ID: trace-me' -H "Authorization: Bearer $TOKEN" \
       -H 'content-type: application/json' -d '{"kind":"note","title":"hi"}' \
       localhost:8000/api/v1/notes -i | head -20
  ```
  Every log line for that request carries `request_id=trace-me`
  ([Ch 8](03-observability.md)).
- **Watch a stream arrive** (`-N` disables curl's buffering, or you'll see it
  all land at once and learn nothing):
  ```bash
  curl -N -X POST localhost:8000/api/v1/chat -H "Authorization: Bearer $TOKEN" \
       -H 'content-type: application/json' \
       -d '{"messages":[{"role":"user","content":"what did I eat today?"}],"timezone":"Asia/Kolkata"}'
  ```
- **Watch journey 2's row change state**, which makes the whole async idea
  concrete:
  ```sql
  SELECT id, kind, status, attempts, run_at FROM jobs ORDER BY id DESC LIMIT 5;
  ```
- **Set `DEBUG=true`** to have SQLAlchemy echo every statement — the honest
  answer to "how many queries does this endpoint make?".

Testing follows the same layers: an `httpx.AsyncClient` over `ASGITransport`
calls the app in-process (no network, no live server), and
`app.dependency_overrides[get_db]` swaps the session for the test one — the
cleanest illustration that dependencies are *seams*, not just parameters. See
[test_auth_google.py](../../backend/tests/test_auth_google.py) and Appendix B
(planned) for why `TestClient` fights the async event loop here.

---

## 14.8 Gotchas

- **Route order matters.** Static segments before `/{param}` ones, or the param
  route eats them (§14.2).
- **`def` instead of `async def` doesn't fail — it just goes slower, in a
  threadpool.** Worse is `async def` containing something *blocking*
  (`requests.get`, `time.sleep`, a heavy CPU loop): that stalls the entire event
  loop, including the background worker and every other user's request.
- **Dependency caching is per request, keyed by the callable.** Two different
  wrappers around `get_db` would give you two sessions and two transactions —
  and a "why can't this transaction see that write?" afternoon.
- **`await db.commit()` is not optional.** No commit, no write: the session
  rolls back at teardown, the handler returns 201, and the row is gone. Silent,
  because nothing errors.
- **`await db.refresh(obj)` after insert** is what fills server-side defaults
  (`id`, `created_at`) — without it, the response can carry `None`s.
- **A mutation to `current_user` is a real DB write once anything commits**,
  since it's a live ORM object in the same session. `record_ai_usage` relies on
  that on purpose; be aware of it when you assign to `current_user.*` casually.
- **After a stream starts, you cannot change the status code.** Validate first,
  then stream (§14.5).
- **`response_model` is a filter, not just documentation.** Adding a field to an
  ORM model doesn't expose it; adding it to the read schema does. That's a
  feature — check the schema before assuming a value is invisible.
- **Don't hand a request-scoped session to background work.** Open a new one.
- **Exceptions inside a generator are not exceptions in your handler.** They
  surface as a truncated response body, which is why `stream_chat` catches its
  own.

---

## 14.9 Where to go next

- [Ch 1 — Auth & sessions](10-auth-and-google-sso.md) — the dependency chain in
  §14.3 in full detail.
- [Ch 8 — Observability](03-observability.md) — the middleware layer, and how one
  `request_id` stitches a journey together.
- [Ch 11 — Agentic tool use](06-ai-chat-tools.md) — what journey 3 is streaming.
- [Ch 13 — Goals dashboard](09-goals-dashboard.md) — a route that mixes free
  deterministic reads with a quota-charged AI call.
- Ch 5 — Background work *(planned)* — the job runner from journey 2 on its own
  terms: schedules, `SKIP LOCKED`, retries, and when an in-process worker stops
  being enough.
