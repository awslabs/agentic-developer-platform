"""Issue #992: Verify RequestLoggingMiddleware is registered in the app."""

from src.admin.middleware import RequestLoggingMiddleware


class TestRequestLoggingMiddlewareRegistered:
    """Confirm that create_app() registers RequestLoggingMiddleware."""

    def test_middleware_is_in_app_stack(self, app):
        """RequestLoggingMiddleware should be present in app.user_middleware."""
        middleware_classes = [m.cls for m in app.user_middleware]
        # The factory returns a subclass of RequestLoggingMiddleware
        assert any(issubclass(cls, RequestLoggingMiddleware) for cls in middleware_classes), (
            f"RequestLoggingMiddleware (or a subclass) must be registered in app.user_middleware. Found: {middleware_classes}"
        )
