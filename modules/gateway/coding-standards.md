# Coding Standards

All agents and contributors MUST follow these standards. Code that violates these standards will be rejected in PR review.

## Python (Backend)

### Style
- Formatter: `ruff format`
- Linter: `ruff check`
- Line length: 120 characters
- Target: Python 3.12+
- Use type hints on all function signatures
- Use `async def` for all database and HTTP operations

### Naming
- Files: `snake_case.py`
- Classes: `PascalCase`
- Functions/methods: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Private methods: `_leading_underscore`

### Imports
- Sort with `ruff` (isort rules)
- Group: stdlib → third-party → local
- Use absolute imports: `from src.shared.models.organization import Organization`

### Error Handling
- Use custom exceptions from `src/shared/exceptions.py`
- Never catch bare `except:` — always specify the exception type
- Log errors with context before re-raising

### Database
- All queries MUST include `org_id` filter for tenant isolation
- Use SQLAlchemy async sessions via `get_db()` dependency
- Never use raw SQL — use SQLAlchemy ORM/Core expressions

### API Routes
- Return Pydantic models, not dicts
- Use FastAPI `Depends()` for dependency injection
- Use HTTP status codes correctly (200, 201, 400, 401, 403, 404, 409, 429, 500, 503)
- Include OpenAPI descriptions on all endpoints

## TypeScript/React (Frontend)

### Style
- Linter: `eslint`
- Formatter: Prettier (via eslint)
- Use TypeScript strict mode
- Use functional components with hooks

### Naming
- Files: `PascalCase.tsx` for components, `camelCase.ts` for utilities
- Components: `PascalCase`
- Functions/hooks: `camelCase`
- Constants: `UPPER_SNAKE_CASE`

## Terraform (Infrastructure)

### Style
- Format: `terraform fmt`
- Validate: `terraform validate`
- Use modules for reusable components
- Use variables for all configurable values — no hardcoded strings

### Naming
- Resources: `snake_case`
- Follow naming convention from `infra-tagging-strategy.md`

## Git

### Branch Naming
- Units: `unit/{unit-name}` (e.g., `unit/auth`, `unit/proxy`)

### Commit Messages
- Format: `type(scope): description`
- Types: `feat`, `fix`, `test`, `docs`, `refactor`, `chore`
- Examples:
  - `feat(auth): implement credential exchange endpoint`
  - `test(budget): add cascading enforcement tests`
  - `docs(cli): add setup instructions`

## Build & Test Requirements (MANDATORY)

### Before Creating a PR, agents MUST:

1. **Lint**: Run linter and fix all issues
   ```bash
   # Backend
   ruff check src/{unit}/ tests/{unit}/ --fix
   ruff format src/{unit}/ tests/{unit}/

   # Frontend
   cd frontend && npm run lint
   ```

2. **Build**: Verify the code compiles/builds
   ```bash
   # Backend — verify imports resolve
   python -c "from src.{unit}.routes import router"

   # Frontend
   cd frontend && npm run build
   ```

3. **Test**: Run ALL unit tests and verify they pass
   ```bash
   # Backend
   pip install -e ".[dev]"
   pytest tests/{unit}/ -v --tb=short

   # Frontend
   cd frontend && npm test
   ```

4. **Coverage**: Aim for >80% coverage on new code
   ```bash
   pytest tests/{unit}/ --cov=src/{unit} --cov-report=term-missing
   ```

5. **No test failures**: ALL tests must pass. Do not create a PR with failing tests.

### If tests fail:
- Fix the code, not the tests
- If a test is wrong, explain why in the PR description
- Never skip or delete tests to make the build pass
