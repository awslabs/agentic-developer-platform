# Contributing to ADP

## Development Workflow

1. Create a feature branch from `main`
2. Make changes in the appropriate module
3. Run tests locally
4. Create a Pull Request
5. CI will run automatically
6. After approval and merge, CD will deploy

## Code Style

- Python: Use `ruff` for linting and formatting
- TypeScript: Use `eslint` and `prettier`
- Terraform: Use `terraform fmt`

## Adding a New Module

```bash
./platform/scripts/add-module.sh <module-name>
```

This creates the module scaffold. You'll need to:

1. Implement your application code
2. Create module-specific Terraform if needed
3. Create GitHub workflows for CI/CD
4. Update platform documentation

## Commit Messages

Use conventional commits:

- `feat(gateway): add rate limiting`
- `fix(agent-runtime): handle timeout errors`
- `docs: update architecture diagram`
- `chore(ci): update workflow`
