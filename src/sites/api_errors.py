"""Ordered HTTP mappings for API exceptions.

Endpoint mixins own request handling, while this module owns the shared exception-to-
response contract without importing the composition root. Entry order is semantic:
specific subclasses must precede ``RuntimeError``.
"""
from __future__ import annotations

from sites.admission import (
    BuildCapacityExceeded,
    BuildNameExists,
    ControlPlaneBusy,
    MerchantQuotaExceeded,
    PublicRouteConflict,
    QuotaExceeded,
    ServiceNameConflict,
)
from sites.kube import ApiError
from sites.object_storage import ObjectStorageError
from sites.storage import StorageConflictError, StorageError
from sites.validation import ValidationError

ErrorResponseEntry = tuple[type[BaseException], int, str | None, str | None]
ErrorResponseTable = tuple[ErrorResponseEntry, ...]

MUTATION_ERROR_RESPONSES: ErrorResponseTable = (
    (ValidationError, 400, None, None),
    (MerchantQuotaExceeded, 429, "merchant_quota_exceeded", None),
    (QuotaExceeded, 429, "quota_exceeded", None),
    (PublicRouteConflict, 409, "public_route_capacity", None),
    (ServiceNameConflict, 409, "service_name_conflict", None),
    (StorageConflictError, 409, "site_version_conflict", None),
    (ControlPlaneBusy, 503, "control_plane_busy", "control plane busy, retry later"),
    (StorageError, 503, None, "database unavailable"),
    (ApiError, 502, None, None),
    (RuntimeError, 502, None, None),
)

BUILD_ERROR_RESPONSES: ErrorResponseTable = (
    (ValidationError, 400, None, None),
    (MerchantQuotaExceeded, 429, "merchant_quota_exceeded", None),
    (QuotaExceeded, 429, "quota_exceeded", None),
    (PublicRouteConflict, 409, "public_route_capacity", None),
    (ServiceNameConflict, 409, "service_name_conflict", None),
    (StorageConflictError, 409, "site_version_conflict", None),
    (BuildNameExists, 409, "build_name_exists", None),
    (BuildCapacityExceeded, 429, "build_capacity", None),
    (ControlPlaneBusy, 503, "control_plane_busy", "control plane busy, retry later"),
    (ObjectStorageError, 503, None, "object storage unavailable"),
    (ApiError, 502, None, None),
    (RuntimeError, 502, None, None),
)
