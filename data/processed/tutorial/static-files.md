# Static Files

You can serve static files automatically from a directory using `StaticFiles`.

**Tip:**

If you need to host a frontend, use `app.frontend()` instead, read about it in [Frontend](frontend.md).

`app.frontend()` uses `StaticFiles` underneath, with several additional advantages for frontends, like handling client-side routing.


## Use `StaticFiles`

* Import `StaticFiles`.
* "Mount" a `StaticFiles()` instance in a specific path.

```python
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
```

**Note:**

You could also use `from starlette.staticfiles import StaticFiles`.

**FastAPI** provides the same `starlette.staticfiles` as `fastapi.staticfiles` just as a convenience for you, the developer. But it actually comes directly from Starlette.


### What is "Mounting"

"Mounting" means adding a complete "independent" application in a specific path, that then takes care of handling all the sub-paths.

This is different from using an `APIRouter` as a mounted application is completely independent. The OpenAPI and docs from your main application won't include anything from the mounted application, etc.

You can read more about this in the [Advanced User Guide](../advanced/index.md).

## Details

The first `"/static"` refers to the sub-path this "sub-application" will be "mounted" on. So, any path that starts with `"/static"` will be handled by it.

The `directory="static"` refers to the name of the directory that contains your static files.

The `name="static"` gives it a name that can be used internally by **FastAPI**.

All these parameters can be different than "`static`", adjust them to the needs and specific details of your own application.

## More info

For more details and options check [Starlette's docs about Static Files](https://starlette.dev/staticfiles/).
