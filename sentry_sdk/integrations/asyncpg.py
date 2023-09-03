import contextlib
from typing import ParamSpec, TypeVar, Callable

from asyncpg.cursor import BaseCursor, CursorIterator

from sentry_sdk import Hub
from sentry_sdk.consts import OP
from sentry_sdk.integrations import Integration, DidNotEnable
from sentry_sdk.tracing_utils import record_sql_queries
from sentry_sdk.utils import parse_version, capture_internal_exceptions

try:
    import asyncpg  # type: ignore[import]

except ImportError:
    raise DidNotEnable("asyncpg not installed.")

# asyncpg.__version__ is a string containing the semantic version in the form of "<major>.<minor>.<patch>"
asyncpg_version = parse_version(asyncpg.__version__)

if asyncpg_version < (0, 23, 0):
    raise DidNotEnable("asyncpg >= 0.23.0 required")


class AsyncPGIntegration(Integration):
    identifier = "asyncpg"

    def __init__(self, *, record_params=False):
        AsyncPGIntegration._record_params = record_params

    @staticmethod
    def setup_once() -> None:
        asyncpg.Connection.execute = _wrap_execute(
            asyncpg.Connection.execute,
        )

        asyncpg.Connection._execute = _wrap_connection_method(
            asyncpg.Connection._execute
        )
        asyncpg.Connection._executemany = _wrap_connection_method(
            asyncpg.Connection._executemany, executemany=True
        )
        asyncpg.connection.cursor.BaseCursor._exec = _wrap_basecursor_exec(
            asyncpg.connection.cursor.BaseCursor._exec
        )
        asyncpg.connection.cursor.CursorIterator.__anext__ = _wrap_cursoriterator_anext(
            asyncpg.connection.cursor.CursorIterator.__anext__
        )
        asyncpg.Connection.prepare = _wrap_connection_method(asyncpg.Connection.prepare)
        asyncpg.connect_utils._connect_addr = _wrap_connect_addr(
            asyncpg.connect_utils._connect_addr
        )


P = ParamSpec("P")
T = TypeVar("T")


def _wrap_execute(f: Callable[P, T]) -> Callable[P, T]:
    async def _inner(*args: P.args, **kwargs: P.kwargs) -> T:
        hub = Hub.current
        integration = hub.get_integration(AsyncPGIntegration)

        # Avoid recording calls to _execute twice.
        # Calls to Connection.execute with args also call
        # Connection._execute, which is recorded separately
        # args[0] = the connection object, args[1] is the query
        if integration is None or len(args) > 2:
            return await f(*args, **kwargs)

        query = args[1]
        with record_sql_queries(hub, None, query, None, None, executemany=False):
            res = await f(*args, **kwargs)
        return res

    return _inner


SubCursor = TypeVar("SubCursor", bound=BaseCursor)


@contextlib.contextmanager
def _record(
    hub: Hub,
    cursor: SubCursor | None,
    query: str,
    params_list: tuple | None,
    *,
    executemany: bool = False
):
    integration = hub.get_integration(AsyncPGIntegration)
    if not integration._record_params:
        params_list = None

    param_style = "pyformat" if params_list else None

    with record_sql_queries(
        hub,
        cursor,
        query,
        params_list,
        param_style,
        executemany=executemany,
        record_cursor_repr=cursor is not None,
    ) as span:
        yield span


def _wrap_connection_method(f: Callable[P, T], *, executemany=False) -> Callable[P, T]:
    async def _inner(*args: P.args, **kwargs: P.kwargs) -> T:
        hub = Hub.current
        integration = hub.get_integration(AsyncPGIntegration)

        if integration is None:
            return await f(*args, **kwargs)

        query = args[1]
        params_list = args[2] if len(args) > 2 else None
        with _record(hub, None, query, params_list, executemany=executemany):
            res = await f(*args, **kwargs)
        return res

    return _inner


def _wrap_basecursor_exec(f: Callable[P, T]) -> Callable[P, T]:
    async def _exec(self: BaseCursor, n, timeout):
        hub = Hub.current
        integration = hub.get_integration(AsyncPGIntegration)

        if integration is None:
            return await f(self, n, timeout)

        params_list = self._args[1] if len(self._args) > 1 else None

        executemany = n > 1

        with _record(
            hub,
            self,
            self._query,
            params_list,
            executemany=executemany,
        ):
            res = await f(self, n, timeout)
        return res

    return _exec


def _wrap_cursoriterator_anext(f: Callable[P, T]) -> Callable[P, T]:
    async def __await__(self: CursorIterator):
        hub = Hub.current
        integration = hub.get_integration(AsyncPGIntegration)

        if integration is None:
            return await f(self)

        params_list = self._args[0]

        with _record(
            hub,
            self,
            self._query,
            params_list,
            executemany=False,
        ) as span:
            try:
                res = await f(self)
            except StopAsyncIteration:

                span.set_data("db.cursor.exhausted", True)
                raise StopAsyncIteration

        return res

    return __await__


def _wrap_connect_addr(f: Callable[P, T]) -> Callable[P, T]:
    async def _inner(*args: P.args, **kwargs: P.kwargs) -> T:
        hub = Hub.current
        integration = hub.get_integration(AsyncPGIntegration)

        if integration is None:
            return await f(*args, **kwargs)

        user = kwargs["params"].user
        database = kwargs["params"].database
        connect_timeout = kwargs["params"].connect_timeout

        with hub.start_span(op=OP.DB, description="connect") as span:
            span.set_data("connection.host", kwargs["addr"])
            span.set_data("connection.user", user)
            span.set_data("connection.database", database)
            span.set_data("connection.connect_timeout", connect_timeout)
            span.set_data("connection.config", repr(kwargs["config"]))
            with capture_internal_exceptions():
                hub.add_breadcrumb(message="connect", category="query", data=span._data)
            res = await f(*args, **kwargs)

        return res

    return _inner
