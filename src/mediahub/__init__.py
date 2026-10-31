"""MediaHub — self-hosted media monitoring dashboard."""

import os

# Release images set MEDIAHUB_VERSION from the git tag; source checkouts report the base version.
__version__ = os.environ.get("MEDIAHUB_VERSION") or "0.4.0"  # x-release-please-version
