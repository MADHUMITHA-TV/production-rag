# Use Old 403 Authentication Error Status Codes

Before FastAPI version `0.122.0`, when the integrated security utilities returned an error to the client after a failed authentication, they used the HTTP status code `403 Forbidden`.

Starting with FastAPI version `0.122.0`, they use the more appropriate HTTP status code `401 Unauthorized`, and return a sensible `WWW-Authenticate` header in the response, following the HTTP specifications, [RFC 7235](https://datatracker.ietf.org/doc/html/rfc7235#section-3.1), [RFC 9110](https://datatracker.ietf.org/doc/html/rfc9110#name-401-unauthorized).

But if for some reason your clients depend on the old behavior, you can revert to it by overriding the method `make_not_authenticated_error` in your security classes.

For example, you can create a subclass of `HTTPBearer` that returns a `403 Forbidden` error instead of the default `401 Unauthorized` error:

```python
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

app = FastAPI()


class HTTPBearer403(HTTPBearer):
    def make_not_authenticated_error(self) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not authenticated"
        )


CredentialsDep = Annotated[HTTPAuthorizationCredentials, Depends(HTTPBearer403())]


@app.get("/me")
def read_me(credentials: CredentialsDep):
    return {"message": "You are authenticated", "token": credentials.credentials}
```

**Tip:**

Notice that the function returns the exception instance, it doesn't raise it. The raising is done in the rest of the internal code.

