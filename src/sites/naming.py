"""Sites naming domain: tenant Namespaces, CR names, and credential minting.

Contains namespace and custom-resource derivation plus token and merchant-key minting and
digests. DNS-label cleanup and identity validation remain in ``validation.py`` to avoid a
circular dependency; this module depends on that module in one direction only.
"""
from __future__ import annotations

import hashlib
import secrets

from sites import exposure as exposure_backend
from sites.validation import (
    dns_label,
    normalize_merchant_id,
    normalize_user_id,
)


TOKEN_PREFIX = "site_"
# The merchant key and the tenant token share the same Authorization header. The prefix is different just for human reading logs.
# Can distinguish which type of certificate it is - the authentication side cannot rely on prefixes to divert traffic. Both tables must be checked, otherwise the prefix becomes
# A bypass signal of "whether this credential exists or not".
MERCHANT_KEY_PREFIX = "sitem_"


def new_tenant_token() -> str:
    """Mint a tenant token. The plaintext only exists at the moment of creation, and thereafter only its digest is in the library."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def new_merchant_api_key() -> str:
    """Mint a merchant API key. Like the tenant token, the clear text only exists at the moment of issuance."""
    return MERCHANT_KEY_PREFIX + secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    """Digest a token for storage and lookup.

    There is no clear text in the library: whoever gets the database should not have easy access to all the tenants' credentials. No salt added here——
    The query is a direct hit by digest, and the token itself is a 32-byte random number, so there is no dictionary attack surface.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def namespace_for_tenant(merchant_id: str, user_id: str) -> str:
    """Derive one tenant's workload Namespace from (merchant, user).

    Constraint: (merchant, user) to Namespace name must be injective - Namespace is isolated
    The boundary itself, two tenants falling into the same one means there is no isolation.

    It cannot be written as dns_label(f"m{merchant}-u{user}-local"): the two hyphens in the middle are on both sides
    You can contribute by your own name. Tenant c of merchant a-ub and tenant b-uc of merchant a will get the same
    name. This is the same type of vulnerability documented by cr_name_for. The prefix is only for easy recognition by operation and maintenance, and is unique
    All guaranteed by digest: the digest input is separated by \\0, and both name fields themselves contain \\0.
    """
    merchant = normalize_merchant_id(merchant_id)
    user = normalize_user_id(user_id)
    # Share the same derivation with GatewayExposure.host_for: if you write one in each place, the consequences of drift will be
    # It is "the namespace is considered to be two tenants, and the host is considered to be one", and that is exactly the cross-tenant traffic leakage.
    digest = exposure_backend.tenant_digest(merchant, user)
    # 40 + 1 + 16 + 6 = 63, which is exactly the upper limit of DNS label.
    prefix = dns_label(f"m{merchant}-u{user}", max_length=40)
    return f"{prefix}-{digest}-local"


def cr_name_for(merchant_id: str, user_id: str, service_name: str) -> str:
    """Derive the control-plane CR name for one tenant's service.

    Constraint: (merchant, user, service) to name must be injective. Input for abstracts is separated by \\0,
    The three name fields themselves do not contain \\0, so different triples cannot get the same digest.

    The early implementation was dns_label(f"{user}-{service}"), the hyphen in the middle can be represented by names on both sides
    Contribute by yourself: The service corp-web of tenant acme and the service web of tenant acme-corp will get the same
    CR name. The CR name is unique in Kubernetes, which is a cross-tenant coverage path - under a single tenant
    It's harmless, opening multi-tenancy is a loophole. The previous readable prefix is ​​only for easy recognition by operation and maintenance, and the uniqueness is guaranteed by the digest.
    """
    merchant = normalize_merchant_id(merchant_id)
    user = normalize_user_id(user_id)
    service = dns_label(service_name)
    digest = hashlib.sha256(
        f"{merchant}\0{user}\0{service}".encode("utf-8")
    ).hexdigest()
    prefix = dns_label(f"{merchant}-{user}-{service}", max_length=46)
    return f"{prefix}-{digest[:16]}"
