"""Platform core: domain models, ports and services.

Rule: nothing in this package imports a concrete technology (database driver,
queue client, gRPC). Adapters implement the ports in ``ports``.
"""
