"""L5 worker integrations with platform-owned APIs."""

__all__ = ["ProductClient"]


def __getattr__(name):
    # Deployment tools use stdlib-only helpers before the worker package and
    # its HTTP dependencies are installed. Preserve the public client export
    # without importing its runtime dependencies for every package submodule.
    if name == "ProductClient":
        from .product_client import ProductClient

        return ProductClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
