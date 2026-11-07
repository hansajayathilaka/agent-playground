"""Domain errors. The API layer maps each one to a gRPC status code."""


class DomainError(Exception):
    """Base class for errors that are safe to show to a client."""


class InvalidArgumentError(DomainError):
    pass


class InvalidPathError(InvalidArgumentError):
    """A client path failed resolve_agent_path()."""


class NotFoundError(DomainError):
    pass


class AlreadyExistsError(DomainError):
    pass


class ConflictError(DomainError):
    """Optimistic concurrency failure (etag mismatch)."""


class PermissionDeniedError(DomainError):
    """The path or operation is reserved for the platform."""


class UnauthenticatedError(DomainError):
    pass
