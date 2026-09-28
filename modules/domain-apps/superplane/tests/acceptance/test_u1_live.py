"""U1-L1: after an authorized deploy/undeploy, the deploy's resources are really gone.

Named by Wave 1 evaluation #5067 and implemented for #5288. This is the teardown half of
U1 acceptance; the feature-API half is `test_u1_features_live.py` (merged separately).

Explicitly marked live: it is deselected by the offline lane's `-m 'not superplane_live'`
and, when invoked directly, FAILS on missing inputs rather than skipping — a skipped
acceptance criterion is the outcome this whole check exists to prevent.
"""

import os

import pytest

from superplane_acceptance.teardown import run_live


@pytest.mark.superplane_live
def test_undeployed_resources_are_absent():
    run_live(os.environ)
