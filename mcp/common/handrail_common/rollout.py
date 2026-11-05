"""Rollback by exact revision (mcp/CONTRACT.md, Rollback).

Pure functions over ReplicaSets and pod templates as the API server returns them (JSON
objects with camelCase keys); nothing here calls the API. A revision is only a name that
lasts until the next rollout: a rollback reuses the old ReplicaSet and gives it a new
number. So a plan pins the ReplicaSet's uid and the hash of its template, and the revision
number stays the approved argument. Pod templates can hold plaintext env values: only
images (as untrusted text) and the names of changed fields leave this module, never values.
"""

import copy
from collections.abc import Iterable, Mapping
from typing import Any

from handrail_common.envelope import INVALID_ARGUMENT, KUBERNETES_ERROR, REVISION_NOT_FOUND
from handrail_common.untrusted import untrusted_text
from handrail_common.validation import GuardError, require_int

REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
CHANGE_CAUSE_ANNOTATION = "kubernetes.io/change-cause"
# Set by the Deployment controller on each ReplicaSet's template; never on the Deployment's.
POD_TEMPLATE_HASH_LABEL = "pod-template-hash"
# A revision is an int64 in the Deployment controller.
MAX_REVISION_DIGITS = 18
MAX_REVISION = 10**MAX_REVISION_DIGITS - 1
# Revisions listed in revision_not_found details, newest first.
AVAILABLE_LIMIT = 20
IMAGE_LIMIT = 20
FIELDS_LIMIT = 50
DIGEST = "@sha256:"
MUTABLE_IMAGE_TAG = "mutable_image_tag"
CONFIG_NOT_ROLLED_BACK = "config_not_rolled_back"
# (spec field, whether its containers are init containers)
CONTAINER_FIELDS = (("containers", False), ("initContainers", True))

Obj = Mapping[str, Any]


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, Mapping):
            return None
        obj = obj.get(key)
    return obj


def parse_revision(value: Any) -> int | None:
    """A revision annotation as an integer, or None when it is not one.

    The annotation is untrusted text: isdigit() alone accepts digits int() rejects, such as
    "²", and the length cap keeps the number within what the record can store.
    """
    if not (isinstance(value, str) and value.isascii() and value.isdigit()):
        return None
    return int(value) if len(value) <= MAX_REVISION_DIGITS else None


def require_revision(value: Any) -> int:
    """The `revision` argument: an integer the revision annotation can hold, at least 1."""
    revision = require_int(value, "revision")
    if not 1 <= revision <= MAX_REVISION:
        raise GuardError(INVALID_ARGUMENT, f"revision must be between 1 and {MAX_REVISION}")
    return revision


def is_controlled_by(obj: Obj, owner_uid: str) -> bool:
    """Whether `obj` has a controller owner reference to `owner_uid`. A plain owner
    reference is not enough: only the controller rolls a ReplicaSet out."""
    return any(
        isinstance(ref, Mapping) and ref.get("controller") is True and ref.get("uid") == owner_uid
        for ref in _get(obj, "metadata", "ownerReferences") or []
    )


def owned_revisions(replica_sets: Iterable[Obj], owner_uid: str) -> list[tuple[int, Obj]]:
    """(revision, ReplicaSet) for each ReplicaSet the Deployment `owner_uid` controls whose
    revision annotation is an integer, newest first."""
    owned = []
    for rs in replica_sets:
        revision = parse_revision(
            (_get(rs, "metadata", "annotations") or {}).get(REVISION_ANNOTATION)
        )
        if revision is not None and is_controlled_by(rs, owner_uid):
            owned.append((revision, rs))
    return sorted(owned, key=lambda pair: (-pair[0], str(_get(pair[1], "metadata", "name"))))


def resolve_revision(replica_sets: Iterable[Obj], owner_uid: str, revision: int) -> Obj:
    """The one ReplicaSet the Deployment controls with this revision."""
    owned = owned_revisions(replica_sets, owner_uid)
    matches = [rs for number, rs in owned if number == revision]
    if not matches:
        raise GuardError(
            REVISION_NOT_FOUND,
            f"revision {revision} has no ReplicaSet controlled by the Deployment; it may have "
            "been pruned by revisionHistoryLimit or renumbered by a rollback",
            {
                "revision": revision,
                "available": [number for number, _ in owned][:AVAILABLE_LIMIT],
            },
        )
    if len(matches) > 1:
        # The controller never does this; refuse rather than pick one.
        raise GuardError(
            KUBERNETES_ERROR,
            f"revision {revision} matches {len(matches)} ReplicaSets of the Deployment",
        )
    return matches[0]


def template_of(replica_set: Obj) -> dict[str, Any]:
    """A copy of the ReplicaSet's pod template without the pod-template-hash label, which is
    the template a Deployment had when it created the ReplicaSet (kubectl rollback removes
    the label the same way). `labels` is dropped if nothing else is in it."""
    template = copy.deepcopy(dict(_get(replica_set, "spec", "template") or {}))
    metadata = template.get("metadata")
    labels = metadata.get("labels") if isinstance(metadata, dict) else None
    if isinstance(labels, dict):
        labels.pop(POD_TEMPLATE_HASH_LABEL, None)
        if not labels:
            del metadata["labels"]
    return template


def _containers(template: Obj) -> dict[tuple[bool, str], Obj]:
    """(init, name) => container, for the containers and init containers of a template."""
    found = {}
    for field, init in CONTAINER_FIELDS:
        for container in _get(template, "spec", field) or []:
            if isinstance(container, Mapping) and isinstance(container.get("name"), str):
                found[(init, container["name"])] = container
    return found


def _pinned(image: Any) -> bool:
    return isinstance(image, str) and DIGEST in image


def container_images(template: Obj) -> tuple[list[dict[str, Any]], bool]:
    """The image of each container, sorted by (init, name), and whether anything was cut.
    Container names are returned plain: the API server validates them as DNS-1123 labels."""
    images = []
    truncated = False
    for (init, name), container in sorted(_containers(template).items()):
        image, cut = untrusted_text(container.get("image"))
        truncated = truncated or cut
        images.append(
            {
                "container": name,
                "init": init,
                "image": image,
                "pinned_by_digest": _pinned(container.get("image")),
            }
        )
    return images[:IMAGE_LIMIT], truncated or len(images) > IMAGE_LIMIT


def image_diff(template_from: Obj, template_to: Obj) -> tuple[list[dict[str, Any]], bool]:
    """The image before and after per container, matched by name, sorted by (init, name),
    and whether anything was cut. A container in only one template has `from` or `to` null;
    `pinned_by_digest` is about the image after."""
    before, after = _containers(template_from), _containers(template_to)
    diff = []
    truncated = False
    for key in sorted(set(before) | set(after)):
        old = (before.get(key) or {}).get("image")
        new = (after.get(key) or {}).get("image")
        wrapped_old, old_cut = untrusted_text(old)
        wrapped_new, new_cut = untrusted_text(new)
        truncated = truncated or old_cut or new_cut
        diff.append(
            {
                "container": key[1],
                "init": key[0],
                "from": wrapped_old,
                "to": wrapped_new,
                "changed": key not in before or key not in after or old != new,
                "pinned_by_digest": _pinned(new),
            }
        )
    return diff[:IMAGE_LIMIT], truncated or len(diff) > IMAGE_LIMIT


def _by_name(containers: Any) -> dict[str, Obj]:
    return {
        c["name"]: c
        for c in containers or []
        if isinstance(c, Mapping) and isinstance(c.get("name"), str)
    }


def fields_changed(template_from: Obj, template_to: Obj) -> tuple[list[str], bool]:
    """The paths whose values differ, at most two levels below the template's `metadata`
    and `spec`, per container matched by name; and whether the list was cut. Names only:
    the values, such as env values, never appear."""
    changed = []
    for part in ("metadata", "spec"):
        old, new = _get(template_from, part) or {}, _get(template_to, part) or {}
        for key in sorted(set(old) | set(new)):
            if part == "spec" and key in dict(CONTAINER_FIELDS):
                changed += _containers_changed(key, old.get(key), new.get(key))
            elif old.get(key) != new.get(key):
                changed.append(f"{part}.{key}")
    return changed[:FIELDS_LIMIT], len(changed) > FIELDS_LIMIT


def _containers_changed(field: str, old: Any, new: Any) -> list[str]:
    before, after = _by_name(old), _by_name(new)
    changed = []
    for name in sorted(set(before) | set(after)):
        if name not in before or name not in after:
            changed.append(f"spec.{field}[{name}]")
            continue
        for key in sorted(set(before[name]) | set(after[name])):
            if before[name].get(key) != after[name].get(key):
                changed.append(f"spec.{field}[{name}].{key}")
    if not changed and old != new:
        # Same containers with the same content, in another order.
        changed.append(f"spec.{field}")
    return changed


def _uses_config(template: Obj) -> bool:
    """Whether the template reads a ConfigMap or Secret: envFrom, env valueFrom, or a
    configMap, secret or projected volume."""
    for container in _containers(template).values():
        if container.get("envFrom"):
            return True
        for env in container.get("env") or []:
            source = _get(env, "valueFrom") or {}
            if "configMapKeyRef" in source or "secretKeyRef" in source:
                return True
    for volume in _get(template, "spec", "volumes") or []:
        if not isinstance(volume, Mapping):
            continue
        if "configMap" in volume or "secret" in volume:
            return True
        sources = _get(volume, "projected", "sources") or []
        if any(isinstance(s, Mapping) and ("configMap" in s or "secret" in s) for s in sources):
            return True
    return False


def rollback_warnings(template_from: Obj, template_to: Obj) -> list[dict[str, str]]:
    """Warnings that only a rollback has, for the approver. They never block the plan."""
    warnings = []
    before, after = _containers(template_from), _containers(template_to)
    mutable = sorted(
        key[1]
        for key, container in after.items()
        if (before.get(key) or {}).get("image") != container.get("image")
        and not _pinned(container.get("image"))
    )
    if mutable:
        warnings.append(
            {
                "code": MUTABLE_IMAGE_TAG,
                "message": f"the target image of {', '.join(mutable)} is not pinned by digest; "
                "the plan pins the image name, and the tag may now point to other content",
            }
        )
    if _uses_config(template_to):
        warnings.append(
            {
                "code": CONFIG_NOT_ROLLED_BACK,
                "message": "the target template reads ConfigMaps or Secrets; a rollback does "
                "not restore their content",
            }
        )
    return warnings
