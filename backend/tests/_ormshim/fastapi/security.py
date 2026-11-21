"""垫片：fastapi.security。"""


class HTTPBearer:
    def __init__(self, auto_error=True):
        self.auto_error = auto_error


class HTTPAuthorizationCredentials:
    def __init__(self, scheme, credentials):
        self.scheme = scheme
        self.credentials = credentials
