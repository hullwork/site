"""Hull Broker: the runtime service that deployed AI SaaS apps call.

It is a data-plane component. Apps depend on it for sign-in and every model call, so
nothing on its request path may go through sites-api or the Kubernetes API. The
cross-partition contract lives in docs/HULL_RUNTIME_CONTRACT.md.
"""
