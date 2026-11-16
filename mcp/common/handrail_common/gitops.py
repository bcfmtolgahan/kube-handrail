"""GitOps ownership detection (guard check 4). Fails closed.

A Deployment is GitOps-managed when its metadata carries a marker that a GitOps controller
sets on the objects it applies:

- Argo CD with annotation tracking (the default since Argo CD 3.0) or annotation+label
  tracking: the annotation `argocd.argoproj.io/tracking-id`.
- Flux: the labels `kustomize.toolkit.fluxcd.io/name` and `/namespace` set by the
  kustomize-controller, and `helm.toolkit.fluxcd.io/name` and `/namespace` set by the
  helm-controller on the objects of a HelmRelease.

`app.kubernetes.io/instance` alone is not a marker: Helm sets it on every chart it
installs. It is also what Argo CD's label tracking writes, so a deployment whose Argo CD
uses label tracking lists it in GITOPS_MARKERS (Terraform `gitops_markers`), as does a
team with any other controller-specific key. A key in that list matches a label or an
annotation of that name, whatever its value.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any

ARGOCD_TRACKING_ANNOTATION = "argocd.argoproj.io/tracking-id"
FLUX_LABELS = (
    "kustomize.toolkit.fluxcd.io/name",
    "kustomize.toolkit.fluxcd.io/namespace",
    "helm.toolkit.fluxcd.io/name",
    "helm.toolkit.fluxcd.io/namespace",
)
# Argo CD label tracking; a marker only when listed in GITOPS_MARKERS.
INSTANCE_LABEL = "app.kubernetes.io/instance"
DEFAULT_ANNOTATIONS = (ARGOCD_TRACKING_ANNOTATION,)
DEFAULT_LABELS = FLUX_LABELS

# A Kubernetes label or annotation key: an optional DNS subdomain prefix and a name of at
# most 63 characters (https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/).
_NAME = r"[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?"
_PREFIX = r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*"
MARKER_KEY = re.compile(rf"^({_PREFIX}/)?{_NAME}$")
MAX_PREFIX_LENGTH = 253


def valid_marker(key: Any) -> bool:
    if not isinstance(key, str) or not MARKER_KEY.fullmatch(key):
        return False
    prefix, _, _ = key.rpartition("/")
    return len(prefix) <= MAX_PREFIX_LENGTH


_MALFORMED = object()


def _metadata(deployment: Any) -> tuple[Any, Any] | object:
    """Return (annotations, labels) from a client model or a plain dict, or _MALFORMED when
    the object has no metadata to read."""
    if isinstance(deployment, Mapping):
        meta = deployment.get("metadata")
        if not isinstance(meta, Mapping):
            return _MALFORMED
        return meta.get("annotations"), meta.get("labels")
    meta = getattr(deployment, "metadata", None)
    if meta is None or isinstance(meta, (str, bytes)):
        return _MALFORMED
    return getattr(meta, "annotations", None), getattr(meta, "labels", None)


def is_gitops_managed(deployment: Any, extra_markers: Iterable[str]) -> bool:
    """True when the Deployment carries a default marker, or a label or annotation named in
    `extra_markers` (Settings.gitops_markers); also when it or its metadata is malformed.

    `extra_markers` has no default, so a caller cannot drop the operator's markers by
    leaving it out."""
    if deployment is None:
        return True
    metadata = _metadata(deployment)
    if metadata is _MALFORMED:
        return True
    annotations, labels = metadata
    if annotations is not None and not isinstance(annotations, Mapping):
        return True
    if labels is not None and not isinstance(labels, Mapping):
        return True
    annotations, labels = annotations or {}, labels or {}
    extra = tuple(extra_markers)
    return any(key in annotations for key in (*DEFAULT_ANNOTATIONS, *extra)) or any(
        key in labels for key in (*DEFAULT_LABELS, *extra)
    )
