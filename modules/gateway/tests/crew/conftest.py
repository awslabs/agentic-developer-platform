"""Share the Crew host app fixture owned by test_dashboard.py with sibling modules.

pytest registers any fixture it finds in a conftest namespace, so importing it here
lets test_keepalive.py request `dashboard` without re-importing a test module.
"""

from tests.crew.test_dashboard import dashboard  # noqa: F401
