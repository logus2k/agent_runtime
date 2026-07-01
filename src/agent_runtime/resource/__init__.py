"""Resource model — the management-plane twin of the Block model.

See ``documents/resource_model.md``. Descriptors declare manageable resources; the editor
renders them with one generic Picker + one generic Manager.
"""

from .descriptor import ResourceDescriptor
from .registry import build_descriptors, descriptor_by_id, descriptors_json
from .sources import list_items

__all__ = [
    "ResourceDescriptor",
    "build_descriptors",
    "descriptor_by_id",
    "descriptors_json",
    "list_items",
]
