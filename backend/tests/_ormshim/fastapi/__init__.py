"""测试专用的最小 fastapi 垫片。"""


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _Route:
    def __init__(self, path, methods, endpoint):
        self.path = path
        self.methods = set(methods)
        self.endpoint = endpoint


class APIRouter:
    def __init__(self, prefix='', tags=None, **kwargs):
        self.prefix = prefix
        self.routes = []

    def _add(self, methods, path, endpoint):
        self.routes.append(_Route(self.prefix + path, methods, endpoint))
        return endpoint

    def get(self, path, *args, **kwargs):
        def deco(fn):
            return self._add(('GET',), path, fn)
        return deco

    def post(self, path, *args, **kwargs):
        def deco(fn):
            return self._add(('POST',), path, fn)
        return deco

    def patch(self, path, *args, **kwargs):
        def deco(fn):
            return self._add(('PATCH',), path, fn)
        return deco

    def put(self, path, *args, **kwargs):
        def deco(fn):
            return self._add(('PUT',), path, fn)
        return deco

    def delete(self, path, *args, **kwargs):
        def deco(fn):
            return self._add(('DELETE',), path, fn)
        return deco


class FastAPI:
    def __init__(self, *args, **kwargs):
        self.routes = []
        self.user_middleware = []

    def include_router(self, router):
        self.routes.extend(router.routes)

    def add_middleware(self, *args, **kwargs):
        pass

    def get(self, path, *args, **kwargs):
        def deco(fn):
            self.routes.append(_Route(path, ('GET',), fn))
            return fn
        return deco


def Depends(fn):
    return fn
