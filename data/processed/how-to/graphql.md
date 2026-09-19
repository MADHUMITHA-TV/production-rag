# GraphQL

As **FastAPI** is based on the **ASGI** standard, it's very easy to integrate any **GraphQL** library also compatible with ASGI.

You can combine normal FastAPI *path operations* with GraphQL on the same application.

**Tip:**

**GraphQL** solves some very specific use cases.

It has **advantages** and **disadvantages** when compared to common **web APIs**.

Make sure you evaluate if the **benefits** for your use case compensate the **drawbacks**. 🤓


## GraphQL Libraries

Here are some of the **GraphQL** libraries that have **ASGI** support. You could use them with **FastAPI**:

* [Strawberry](https://strawberry.rocks/) 🍓
    * With [docs for FastAPI](https://strawberry.rocks/docs/integrations/fastapi)
* [Ariadne](https://ariadnegraphql.org/)
    * With [docs for FastAPI](https://ariadnegraphql.org/server/Integrations/fastapi-integration)
* [Tartiflette](https://tartiflette.io/)
    * With [Tartiflette ASGI](https://tartiflette.github.io/tartiflette-asgi/) to provide ASGI integration
* [Graphene](https://graphene-python.org/)
    * With [starlette-graphene3](https://github.com/ciscorn/starlette-graphene3)

## GraphQL with Strawberry

If you need or want to work with **GraphQL**, [**Strawberry**](https://strawberry.rocks/) is the **recommended** library as it has the design closest to **FastAPI's** design, it's all based on **type annotations**.

Depending on your use case, you might prefer to use a different library, but if you asked me, I would probably suggest you try **Strawberry**.

Here's a small preview of how you could integrate Strawberry with FastAPI:

```python
import strawberry
from fastapi import FastAPI
from strawberry.fastapi import GraphQLRouter


@strawberry.type
class User:
    name: str
    age: int


@strawberry.type
class Query:
    @strawberry.field
    def user(self) -> User:
        return User(name="Patrick", age=100)


schema = strawberry.Schema(query=Query)


graphql_app = GraphQLRouter(schema)

app = FastAPI()
app.include_router(graphql_app, prefix="/graphql")
```

You can learn more about Strawberry in the [Strawberry documentation](https://strawberry.rocks/).

And also the docs about [Strawberry with FastAPI](https://strawberry.rocks/docs/integrations/fastapi).

## Older `GraphQLApp` from Starlette

Previous versions of Starlette included a `GraphQLApp` class to integrate with [Graphene](https://graphene-python.org/).

It was deprecated from Starlette, but if you have code that used it, you can easily **migrate** to [starlette-graphene3](https://github.com/ciscorn/starlette-graphene3), that covers the same use case and has an **almost identical interface**.

**Tip:**

If you need GraphQL, I still would recommend you check out [Strawberry](https://strawberry.rocks/), as it's based on type annotations instead of custom classes and types.


## Learn More

You can learn more about **GraphQL** in the [official GraphQL documentation](https://graphql.org/).

You can also read more about each of those libraries described above in their links.
