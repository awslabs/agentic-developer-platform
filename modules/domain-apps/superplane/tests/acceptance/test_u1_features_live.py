"""U1 feature API sub-evidence only; teardown acceptance is a separate criterion."""

import os

import pytest

from superplane_acceptance.features import run_live


@pytest.mark.superplane_live
def test_authenticated_feature_api_is_currently_disabled():
    run_live(os.environ)
