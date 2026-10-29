class IdentityError(Exception):
    """An identity failure safe to map at the presentation boundary."""


class AuthenticationFailed(IdentityError):
    pass


class AuthorizationDenied(IdentityError):
    pass


class UserAlreadyExists(IdentityError):
    pass


class UserNotFound(IdentityError):
    pass


class InvalidIdentityInput(IdentityError):
    pass


class IdentityUnavailable(IdentityError):
    pass
