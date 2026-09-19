# Frontend

You can serve static frontend apps with `app.frontend()` (or `router.frontend()`).

This is useful for frontend tools that generate static files, like React with Vite, TanStack Router, Astro, Vue, Svelte, Angular, Solid, and others.

With these tools, you normally have a step that builds the frontend, with a command like:

```bash
npm run build
```

That would generate a directory like `./dist/` with your frontend files.

You can use `app.frontend()` to serve that directory following the conventions needed by these frontend frameworks.

**FastAPI** checks *path operations* first. The frontend files are checked only if no normal route matched, so your API won't be affected.

## Serve a Frontend

After building your frontend, for example with `npm run build`, put the generated files in a directory, for example, `dist`.

Your project structure could look like this:

```text
.
├── pyproject.toml
├── app
│   ├── __init__.py
│   └── main.py
└── dist
    ├── index.html
    └── assets
        └── app.js
```

Then serve it with `app.frontend()`:

```python
from fastapi import FastAPI

app = FastAPI()

app.frontend("/", directory="dist")
```

With this, a request for `/assets/app.js` can serve `dist/assets/app.js`.

If you also have a **FastAPI** *path operation*, the *path operation* wins.

## Client-Side Routing

Many frontend apps, including **single-page apps** (SPAs), use client-side routing. A path like `/dashboard/settings` might not be a real file but the framework would take care of handling it.

So, if accessing that URL directly (instead of navigating through the app), the backend should serve the frontend app from `index.html`, so that the frontend framework can then handle the client-side routing.

For that, use `fallback="index.html"`:

```python
from fastapi import FastAPI

app = FastAPI()

app.frontend("/", directory="dist", fallback="index.html")
```

**FastAPI** uses this fallback only for `GET` and `HEAD` requests that explicitly accept HTML with `Accept: text/html` or `Accept: application/xhtml+xml`, as browser navigation requests normally do. Missing files like JavaScript, CSS, and images still return `404`.

Requests with other methods, like `POST` or `PUT`, to paths that only match the frontend fallback also return `404`. Regular **FastAPI** *path operations* still have higher priority than frontend routes.

**Tip:**

By default, `fallback` has a value of `fallback="auto"`. In most cases you won't need to specify `fallback`. Read below for details.


This is what you would want with many frontend apps that use client-side routing, for example, React with TanStack Router, Vue, Angular, SvelteKit, or Solid.

## Custom 404 Page

You can also serve a static `404.html` page for missing frontend paths:

```python
from fastapi import FastAPI

app = FastAPI()

app.frontend("/", directory="dist", fallback="404.html")
```

That response keeps a status code of `404`.

In this case, **FastAPI** won't serve `index.html` for missing frontend paths. It will return the `404.html` file instead.

**Tip:**

By default, `fallback` has a value of `fallback="auto"`. With this, if a `404.html` file is found, it will be used as the fallback automatically.

So, you can normally omit the `fallback` argument.


This is useful with frontend tools that generate static HTML files for each page, like Astro.

## Fallback Auto

By default, `app.frontend()` uses `fallback="auto"`.

If there is a `404.html` file in the frontend directory, missing frontend paths serve that file with status code `404`.

Otherwise, if there is an `index.html` file, missing browser navigation paths serve `index.html`, which is what many frontend apps with client-side routing expect.

So, in most cases you can use `app.frontend("/", directory="dist")` without specifying the `fallback` argument.

```python
from fastapi import FastAPI

app = FastAPI()

app.frontend("/", directory="dist")
```

## Disable Fallback

If you don't want to serve a fallback file for missing frontend paths, use `fallback=None`:

```python
from fastapi import FastAPI

app = FastAPI()

app.frontend("/", directory="dist", fallback=None)
```

Then missing frontend paths return the normal `404`.

## Check Directory

By default, `app.frontend()` uses `check_dir="auto"`.

When the `FASTAPI_ENV` environment variable is set to `development`, **FastAPI** only shows a warning if the frontend build output directory is missing. The [`fastapi dev` command](https://github.com/fastapi/fastapi-cli#fastapi-dev) sets this environment variable for you if it is not already set. This lets you start the backend before building or starting the frontend during development.

In any other environment, **FastAPI** raises an error when the app is created. This helps catch configuration errors early before deploying an app without its frontend files.

You can also set `check_dir=True` to always check the directory when the app is created.

If your frontend files are created later, for example by a separate build step after the app object is created, set `check_dir=False`:

```python
from fastapi import FastAPI

app = FastAPI()

app.frontend("/", directory="dist", check_dir=False)
```

With `check_dir=False`, **FastAPI** will not check the directory when the app is created. If the configured directory is still missing when a request is handled, **FastAPI** will raise an error then.

## Use it with `APIRouter`

You can also add frontend files to an `APIRouter` and include it with a prefix:

```python
from fastapi import APIRouter, FastAPI

app = FastAPI()
router = APIRouter()

router.frontend("/", directory="dist", fallback="index.html")
app.include_router(router, prefix="/app")
```

In this example, frontend paths are served under `/app`.

Any regular *path operations* in the app will still take precedence, including in other routers.

## Dependencies and Middleware

Frontend responses run inside the normal **FastAPI** application, so HTTP middleware applies to them.

Dependencies from the app, from an `APIRouter`, and from `include_router()` also apply to frontend responses. This can be useful for protecting a frontend with cookie authentication or similar.

Dependencies can also modify response headers and add background tasks, as with normal *path operations*.

## Static Build Output Only

`app.frontend()` serves files already generated by your frontend build.

It does not run server-side rendering. It is for frontend frameworks that generate static files, not for frameworks that need dynamic rendering on the server for each request.
