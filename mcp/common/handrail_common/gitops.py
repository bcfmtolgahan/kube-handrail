"""GitOps ownership detection (guard check 4). Fails closed."""

from collections.abc import Mapping
from typing import Any

ARGOCD_TRACKING_ANNOTATION = "argocd.argoproj.io/tracking-id"
INSTANCE_LABEL = "app.kubernetes.io/instance"


def _metadata(deployment: Any) -> tuple[Any, Any]:
    """Return (annotations, labels) from a client model or a plain dict."""
    if isinstance(deployment, Mapping):
        meta = deployment.get("metadata")
        if isinstance(meta, Mapping):
            return meta.get("annotations"), meta.get("labels")
        return None, None
    meta = getattr(deployment, "metadata", None)
    return getattr(meta, "annotations", None), getattr(meta, "labels", None)


def is_gitops_managed(deployment: Any) -> bool:
    if deployment is None:
        return True
    annotations, labels = _metadata(deployment)
    if annotations is not None and not isinstance(annotations, Mapping):
        return True
    if labels is not None and not isinstance(labels, Mapping):
        return True
    return ARGOCD_TRACKING_ANNOTATION in (annotations or {}) or INSTANCE_LABEL in (labels or {})
