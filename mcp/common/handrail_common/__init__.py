"""Shared code for the kube-handrail Lambda handlers."""

# The release version, the single source for the User-Agent the Lambdas send to the
# Kubernetes API (handrail_common.eks). Equal to pyproject.toml's version and to the newest
# release in CHANGELOG.md (tests/unit/test_version.py); docs/releasing.md bumps it.
__version__ = "0.0.0"
