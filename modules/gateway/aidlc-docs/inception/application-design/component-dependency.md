# Component Dependencies

## Dependency Matrix

| Component | Depends On | Depended On By |
|-----------|-----------|----------------|
| **Shared Foundation** | (none) | ALL components |
| **Auth** | Shared Foundation | Proxy, Admin, all middleware |
| **Proxy** | Shared Foundation, Pool | (API consumers) |
| **Budget** | Shared Foundation | Proxy (via middleware), Admin |
| **Rate Limiting** | Shared Foundation | Proxy (via middleware), Admin |
| **Pool** | Shared Foundation | Proxy |
| **Admin API** | Shared Foundation, Auth, Budget, Rate Limiting, Usage | Admin UI |
| **Usage** | Shared Foundation | Proxy (via middleware), Admin |
| **Admin UI** | Admin API (HTTP) | (browser users) |
| **Infrastructure** | (none — Terraform) | All (deployment target) |
| **CLI Tools** | Auth API (HTTP) | (end users) |

## Dependency Diagram

```
                    +-------------------+
                    | Shared Foundation |
                    | (models, schemas, |
                    |  interfaces, utils)|
                    +--------+----------+
                             |
            +-------+--------+--------+--------+--------+
            |       |        |        |        |        |
            v       v        v        v        v        v
         +-----+ +-----+ +------+ +------+ +-----+ +------+
         |Auth | |Proxy| |Budget| |Rate  | |Pool | |Usage |
         |     | |     | |      | |Limit | |     | |      |
         +--+--+ +--+--+ +--+---+ +--+---+ +--+--+ +--+---+
            |       |        |        |        |        |
            |       +--------+--------+--------+        |
            |       | (via middleware)                   |
            |       v                                   |
            |    +--+--+                                |
            |    |Proxy|                                |
            |    |Route|                                |
            |    +-----+                                |
            |                                           |
            +----------+----------+----------+----------+
                       |          |          |
                       v          v          v
                    +--+----------+----------+--+
                    |        Admin API           |
                    +------------+---------------+
                                 |
                                 | (HTTP)
                                 v
                    +------------+---------------+
                    |        Admin UI            |
                    | (React + Tailwind)         |
                    +----------------------------+

    Separate:
    +------------+          +-------------------+
    | CLI Tools  |--HTTP--->| Auth API          |
    | (bg-auth)  |          | (POST /auth/      |
    +------------+          |  exchange)         |
                            +-------------------+

    +-------------------+
    | Infrastructure    |
    | (Terraform)       |
    | - EKS, RDS, ALB   |
    +-------------------+
```

## Communication Patterns

| From | To | Pattern | Protocol |
|------|----|---------|----------|
| Middleware → Services | In-process | Direct function call | Python |
| Route Handlers → Services | In-process | Dependency injection | Python |
| Admin UI → Admin API | Network | REST over HTTPS | HTTP |
| CLI Tools → Auth API | Network | REST over HTTPS | HTTP |
| Pool Service → Bedrock | Network | AWS SDK (STS AssumeRole + InvokeModel) | HTTPS |
| Auth Service → STS | Network | AWS SDK (GetCallerIdentity) | HTTPS |
| Services → PostgreSQL | Network | SQLAlchemy async | TCP |
| Rate Limit → Redis | Network (optional) | redis-py async | TCP |

## Shared Foundation — What Gets Committed Before Agents Start

The following MUST be in `main` branch before any agent begins work:

```
src/
  shared/
    __init__.py
    models/              # SQLAlchemy ORM models (all tables)
      __init__.py
      base.py            # Base model with org_id, timestamps
      organization.py
      user.py
      token.py
      budget.py
      usage.py
      pool.py
    schemas/             # Pydantic request/response schemas
      __init__.py
      auth.py
      proxy.py
      budget.py
      ratelimit.py
      admin.py
      usage.py
      pool.py
    interfaces/          # Abstract base classes for services
      __init__.py
      auth.py
      proxy.py
      budget.py
      ratelimit.py
      pool.py
      usage.py
    config.py            # Settings (Pydantic BaseSettings)
    exceptions.py        # Custom exception classes with error codes
    utils.py             # hash_token, generate_token, calculate_cost
    database.py          # Async engine, session factory
  app.py                 # FastAPI app factory (empty routes, middleware wiring)
alembic/                 # Database migrations
  alembic.ini
  env.py
  versions/
    001_initial.py       # All tables
pyproject.toml           # Project dependencies
Dockerfile               # Multi-stage build
docker-compose.yml       # Local dev (PostgreSQL + Redis + app)
```

This shared foundation defines the contracts. Each unit implements against these interfaces and models without needing other units present.
