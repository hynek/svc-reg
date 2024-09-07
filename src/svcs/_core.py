# SPDX-FileCopyrightText: 2023 Hynek Schlawack <hs@ox.cx>
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import inspect
import logging
import warnings

from collections.abc import Callable
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    asynccontextmanager,
    contextmanager,
    suppress,
)
from inspect import (
    isasyncgenfunction,
    isawaitable,
    iscoroutine,
    iscoroutinefunction,
    isgeneratorfunction,
)
from types import TracebackType
from typing import Any, Awaitable, Iterator, TypeVar, overload

import attrs

from .exceptions import ServiceNotFoundError


log = logging.getLogger("svcs")


def _full_name(obj: object) -> str:
    try:
        return f"{obj.__module__}.{obj.__qualname__}"  # type: ignore[attr-defined]
    except AttributeError:
        return repr(obj)


# Default names where to put the container and registry in integrations.
_KEY_REGISTRY = "svcs_registry"
_KEY_CONTAINER = "svcs_container"


@attrs.frozen(repr=False)
class RegisteredService:
    svc_type: type
    factory: Callable = attrs.field(hash=False)
    takes_container: bool
    enter: bool
    ping: Callable | None = attrs.field(hash=False)

    @property
    def name(self) -> str:
        return _full_name(self.svc_type)

    def __repr__(self) -> str:
        return (
            f"<RegisteredService(svc_type="
            f"{self.name}, "
            f"factory={self.factory}, "
            f"takes_container={self.takes_container}, "
            f"enter={self.enter}, "
            f"has_ping={ self.ping is not None}"
            ")>"
        )


@attrs.frozen
class ServicePing:
    """
    A service health check as returned by :meth:`svcs.Container.get_pings`.

    Attributes:
        name: A fully-qualified name of the service type.

        is_async: Whether the service needs to be pinged using :meth:`aping`.

    See Also:
        :ref:`health`
    """

    name: str
    is_async: bool
    _svc_type: type
    _ping: Callable
    _container: Container

    def ping(self) -> None:
        """
        Acquire the service, schedule its cleanup, and call its ping callable
        with the acquired service as its only argument.
        """
        svc: Any = self._container.get(self._svc_type)
        self._ping(svc)

    async def aping(self) -> None:
        """
        Same as :meth:`ping` but acquire and/or ping asynchronously, if
        necessary.

        Also works with synchronous services, so in an async application, just
        use this.
        """
        svc: Any = await self._container.aget(self._svc_type)
        if self.is_async:
            await self._ping(svc)
        else:
            self._ping(svc)


@attrs.define(repr=False)
class Registry:
    """
    A central registry of recipes for creating services.

    An instance of this should live as long as your application does.

    Also works as a context manager that runs ``on_registry_close`` callbacks
    on exit:

    .. doctest::

        >>> import svcs
        >>> with svcs.Registry() as reg:
        ...     reg.register_value(
        ...         int, 42,
        ...         on_registry_close=lambda: print("closed!")
        ...     )
        closed!

    ``async with`` is also supported.

    Warns:
        ResourceWarning:
            If a registry with pending cleanups is garbage-collected.
    """

    _services: dict[type, RegisteredService] = attrs.Factory(dict)
    _on_close: list[tuple[str, Callable | Awaitable]] = attrs.Factory(list)

    def __repr__(self) -> str:
        return f"<svcs.Registry(num_services={len(self._services)})>"

    def __contains__(self, svc_type: type) -> bool:
        """
        Check whether this registry knows how to create *svc_type*:

        .. doctest::

            >>> reg = svcs.Registry()
            >>> reg.register_value(int, 42)
            >>> int in reg
            True
            >>> str in reg
            False
        """
        return svc_type in self._services

    def __enter__(self) -> Registry:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    async def __aenter__(self) -> Registry:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __del__(self) -> None:
        """
        Warn if the registry is gc'ed before being closed.
        """
        if getattr(self, "_on_close", None):
            warnings.warn(
                "Registry was garbage-collected with pending cleanups.",
                ResourceWarning,
                stacklevel=1,
            )

    def __iter__(self) -> Iterator[type]:
        """Iterate over the registered services types."""
        return iter(self._services)

    def register_factory(
        self,
        svc_type: type,
        factory: Callable,
        *,
        enter: bool = True,
        ping: Callable | None = None,
        on_registry_close: Callable | Awaitable | None = None,
    ) -> None:
        """
        Register *factory* to be used when asked for a *svc_type*.

        Repeated registrations overwrite previous ones, but the
        *on_registry_close* callbacks are run all together when the registry is
        closed.

        Args:
            svc_type: The type of the service to register.

            factory:
                A callable that is used to instantiated *svc_type* if asked. If
                it's a generator or a context manager, a cleanup is registered
                after instantiation.

                Can also be an async callable/generator/context manager.

                If *factory* takes a first argument called ``svcs_container``
                or the first argument (of any name) is annotated as being
                :class:`svcs.Container`, the container instance that is
                instantiating the service is passed into the factory as the
                first positional argument.

                Note:
                    Generally speaking, given the churn and edgecases in the
                    typing ecosystem, we recommend using the name route to
                    detect the container argument because it's most reliable.

            enter:
                Whether to enter context managers if one is returned by
                *factory*. Usually you want that, but there are occasions --
                like database transaction managers -- that you want to enter
                manually.

            ping:
                A callable that marks the service as having a health check.

                See Also:
                    :meth:`Container.get_pings` and :class:`ServicePing`.

            on_registry_close:
                A callable that is called when the
                :meth:`svcs.Registry.close()` method is called.

                Can also be an async callable or an
                :class:`collections.abc.Awaitable`; then
                :meth:`svcs.Registry.aclose()` must be called.
        """
        rs = self._register_factory(
            svc_type,
            factory,
            enter=enter,
            ping=ping,
            on_registry_close=on_registry_close,
        )

        log.debug(
            "registered factory %r for service type %s",
            factory,
            rs.name,
            extra={
                "svcs_service_name": rs.name,
                "svcs_factory_name": _full_name(factory),
            },
            stack_info=True,
        )

    def register_value(
        self,
        svc_type: type,
        value: object,
        *,
        enter: bool = False,
        ping: Callable | None = None,
        on_registry_close: Callable | Awaitable | None = None,
    ) -> None:
        """
        Syntactic sugar for::

           register_factory(
               svc_type,
               lambda: value,
               enter=enter,
               ping=ping,
               on_registry_close=on_registry_close
           )

        Please note that, unlike with :meth:`register_factory`, entering
        context managers is **disabled** by default.

        .. versionchanged:: 23.21.0
           *enter* is now ``False`` by default.
        """
        rs = self._register_factory(
            svc_type,
            lambda: value,
            enter=enter,
            ping=ping,
            on_registry_close=on_registry_close,
        )

        log.debug(
            "registered value %r for service type %s",
            value,
            rs.name,
            extra={"svcs_service_name": rs.name, "svcs_value": value},
            stack_info=True,
        )

    def _register_factory(
        self,
        svc_type: type,
        factory: Callable,
        enter: bool,
        ping: Callable | None,
        on_registry_close: Callable | Awaitable | None = None,
    ) -> RegisteredService:
        if isgeneratorfunction(factory):
            factory = contextmanager(factory)
        elif isasyncgenfunction(factory):
            factory = asynccontextmanager(factory)

        rs = RegisteredService(
            svc_type, factory, _takes_container(factory), enter, ping
        )
        self._services[svc_type] = rs
        if on_registry_close is not None:
            self._on_close.append((rs.name, on_registry_close))
        return rs

    def get_registered_service_for(self, svc_type: type) -> RegisteredService:
        try:
            return self._services[svc_type]
        except KeyError:
            raise ServiceNotFoundError(svc_type) from None

    def close(self) -> None:
        """
        Clear registrations and run synchronous *on_registry_close* callbacks.

        Async callbacks are *not* awaited and a warning is raised

        Errors are logged at warning level, but otherwise ignored.
        """
        for name, oc in reversed(self._on_close):
            if iscoroutinefunction(oc) or isawaitable(oc):
                warnings.warn(
                    f"Skipped async cleanup for {name!r}. "
                    "Use aclose() instead.",
                    # stacklevel doesn't matter here; it's coming from a
                    # framework.
                    stacklevel=1,
                )
                continue

            try:
                log.debug("closing %r", name)
                oc()
                log.debug("closed %r", name)
            except Exception:  # noqa: BLE001
                log.warning(
                    "Registry's on_registry_close callback failed for %r.",
                    name,
                    exc_info=True,
                    extra={"svcs_service_name": name},
                )

        self._services.clear()
        self._on_close.clear()

    async def aclose(self) -> None:
        """
        Clear registrations and run all *on_registry_close* callbacks.

        Errors are logged at warning level, but otherwise ignored.

        Also works with synchronous services, so in an async application, just
        use this.
        """
        for name, oc in reversed(self._on_close):
            try:
                if iscoroutinefunction(oc):
                    oc = oc()  # noqa: PLW2901

                if isawaitable(oc):
                    log.debug("async closing %r", name)
                    await oc
                    log.debug("async closed %r", name)
                else:
                    log.debug("closing %r", name)
                    oc()
                    log.debug("closed %r", name)
            except Exception:  # noqa: BLE001, PERF203
                log.warning(
                    "Registry's on_registry_close callback failed for %r.",
                    name,
                    exc_info=True,
                    extra={"svcs_service_name": name},
                )

        self._services.clear()
        self._on_close.clear()


def _takes_container(factory: Callable) -> bool:
    """
    Return True if *factory* takes a svcs.Container as its first argument.
    """
    try:
        # Provide the locals so that `eval_str` will work even if the user places the `Container`
        # under a `if TYPE_CHECKING` block
        sig = inspect.signature(
            factory, locals={"Container": Container}, eval_str=True
        )
    except Exception:  # noqa: BLE001
        # Retry without `eval_str` since if the annotation is "svcs.Container" the eval
        # will fail due to it not finding the `svcs` module
        try:
            sig = inspect.signature(factory)
        except Exception:  # noqa: BLE001
            return False

    if not sig.parameters:
        return False

    if len(sig.parameters) != 1:
        msg = "Factories must take 0 or 1 parameters."
        raise TypeError(msg)

    ((name, p),) = tuple(sig.parameters.items())

    return name == "svcs_container" or p.annotation in (
        Container,
        "svcs.Container",
        "Container",
    )


T1 = TypeVar("T1")
T2 = TypeVar("T2")
T3 = TypeVar("T3")
T4 = TypeVar("T4")
T5 = TypeVar("T5")
T6 = TypeVar("T6")
T7 = TypeVar("T7")
T8 = TypeVar("T8")
T9 = TypeVar("T9")
T10 = TypeVar("T10")


@attrs.define(repr=False)
class Container:
    """
    A per-context container for instantiated services and cleanups.

    The instance of this should live as long as a request or a task.

    Also works as a context manager that runs clean ups on exit:

    .. doctest::

        >>> reg = svcs.Registry()
        >>> def factory() -> str:
        ...     yield "Hello World"
        ...     print("Cleaned up!")
        >>> reg.register_factory(str, factory)

        >>> with svcs.Container(reg) as con:
        ...     _ = con.get(str)
        Cleaned up!

    Warns:
        ResourceWarning:
            If a container with pending cleanups is garbage-collected.

    Attributes:
        registry:
            The :class:`Registry` instance that this container uses for service
            type lookup.

    """

    registry: Registry
    _lazy_local_registry: Registry | None = None
    _instantiated: dict[type, object] = attrs.Factory(dict)
    _on_close: list[
        tuple[str, AbstractContextManager | AbstractAsyncContextManager]
    ] = attrs.Factory(list)

    def __repr__(self) -> str:
        return (
            f"<Container(instantiated={len(self._instantiated)}, "
            f"cleanups={len(self._on_close)})>"
        )

    def __contains__(self, svc_type: type) -> bool:
        """
        Check whether this container has a cached instance of *svc_type*.
        """
        return svc_type in self._instantiated

    def __enter__(self) -> Container:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    async def __aenter__(self) -> Container:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __del__(self) -> None:
        """
        Warn if the container is gc'ed before being closed.
        """
        if getattr(self, "_on_close", None):
            warnings.warn(
                "Container was garbage-collected with pending cleanups.",
                ResourceWarning,
                stacklevel=1,
            )

    def close(self) -> None:
        """
        Run all registered *synchronous* cleanups.

        Async closes are *not* awaited and a warning is raised.

        Errors are logged at warning level, but otherwise ignored.

        Hint:
            The Container can be used again after this. Closing it is an
            idempotent way to reset it.
        """
        for name, cm in reversed(self._on_close):
            try:
                if isinstance(cm, AbstractAsyncContextManager):
                    warnings.warn(
                        f"Skipped async cleanup for {name!r}. "
                        "Use aclose() instead.",
                        # stacklevel doesn't matter here; it's coming from a
                        # framework.
                        stacklevel=1,
                    )
                    continue

                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                log.warning(
                    "Container clean up failed for %r.",
                    name,
                    exc_info=True,
                    extra={"svcs_service_name": name},
                )

        if self._lazy_local_registry is not None:
            self._lazy_local_registry.close()
        self._on_close.clear()
        self._instantiated.clear()

    async def aclose(self) -> None:
        """
        Run *all* registered cleanups -- synchronous **and** asynchronous.

        Errors are logged at warning level, but otherwise ignored.

        Also works with synchronous services, so in an async application, just
        use this.

        Hint:
            The container can be used again after this. Closing it is an
            idempotent way to reset it.
        """
        for name, cm in reversed(self._on_close):
            try:
                if isinstance(cm, AbstractContextManager):
                    cm.__exit__(None, None, None)
                else:
                    await cm.__aexit__(None, None, None)

            except Exception:  # noqa: BLE001, PERF203
                log.warning(
                    "Container clean up failed for %r.",
                    name,
                    exc_info=True,
                    extra={"svcs_service_name": name},
                )

        if self._lazy_local_registry is not None:
            await self._lazy_local_registry.aclose()
        self._on_close.clear()
        self._instantiated.clear()

    def get_pings(self) -> list[ServicePing]:
        """
        Return all services that have defined a *ping* and bind them to this
        container.

        Returns:
            A list of services that have registered a ping callable.
        """
        pings = []
        registered_local_svc_types = set()
        if self._lazy_local_registry is not None:
            svcs = (
                self._lazy_local_registry.get_registered_service_for(tp)
                for tp in self._lazy_local_registry
            )
            for rs in svcs:
                if rs.ping is not None:
                    pings.append(
                        ServicePing(
                            rs.name,
                            iscoroutinefunction(rs.ping),
                            rs.svc_type,
                            rs.ping,
                            self,
                        )
                    )
                    registered_local_svc_types.add(rs.svc_type)
        svcs = (
            self.registry.get_registered_service_for(tp)
            for tp in self.registry
        )
        pings.extend(
            ServicePing(
                rs.name,
                iscoroutinefunction(rs.ping),
                rs.svc_type,
                rs.ping,
                self,
            )
            for rs in svcs
            if rs.ping is not None
            and rs.svc_type not in registered_local_svc_types
        )
        return pings

    def get_abstract(self, *svc_types: type) -> Any:
        """
        Like :meth:`get` but is annotated to return :data:`typing.Any` which
        allows it to be used with abstract types like :class:`typing.Protocol`
        or :mod:`abc` classes.

        Note:
             See :doc:`typing-caveats` why this is necessary.
        """
        return self.get(*svc_types)

    async def aget_abstract(self, *svc_types: type) -> Any:
        """
        Same as :meth:`get_abstract` but instantiates asynchronously, if
        necessary.

        Also works with synchronous services, so in an async application, just
        use this.
        """
        return await self.aget(*svc_types)

    def _lookup(self, svc_type: type) -> tuple[bool, object, str, bool]:
        """
        Look up svc_type first in our cache, then in the registry.

        If it's cached, only the first two items of the returned tupled are
        meaningful.
        """
        if (
            svc := self._instantiated.get(svc_type, attrs.NOTHING)
        ) is not attrs.NOTHING:
            return True, svc, "", False

        rs = None
        if self._lazy_local_registry is not None:
            with suppress(ServiceNotFoundError):
                rs = self._lazy_local_registry.get_registered_service_for(
                    svc_type
                )

        if rs is None:
            rs = self.registry.get_registered_service_for(svc_type)

        svc = rs.factory(self) if rs.takes_container else rs.factory()

        return False, svc, rs.name, rs.enter

    def register_local_factory(
        self,
        svc_type: type,
        factory: Callable,
        *,
        enter: bool = True,
        ping: Callable | None = None,
        on_registry_close: Callable | Awaitable | None = None,
    ) -> None:
        """
        Same as :meth:`svcs.Registry.register_factory()`, but registers the
        factory only for this container.

        A temporary :class:`svcs.Registry` is transparently created -- and
        closed together with the container it belongs to.

        See Also:
            :ref:`local-registries`

        .. versionadded:: 23.21.0
        """
        if self._lazy_local_registry is None:
            self._lazy_local_registry = Registry()

        self._lazy_local_registry.register_factory(
            svc_type=svc_type,
            factory=factory,
            enter=enter,
            ping=ping,
            on_registry_close=on_registry_close,
        )

    def register_local_value(
        self,
        svc_type: type,
        value: object,
        *,
        enter: bool = False,
        ping: Callable | None = None,
        on_registry_close: Callable | Awaitable | None = None,
    ) -> None:
        """
        Syntactic sugar for::

           register_local_factory(
               svc_type,
               lambda: value,
               enter=enter,
               ping=ping,
               on_registry_close=on_registry_close
           )

        Please note that, unlike with :meth:`register_local_factory`, entering
        context managers is **disabled** by default.

        See Also:
            :ref:`local-registries`

        .. versionadded:: 23.21.0
        """
        self.register_local_factory(
            svc_type,
            lambda: value,
            enter=enter,
            ping=ping,
            on_registry_close=on_registry_close,
        )

    @overload
    def get(self, svc_type: type[T1], /) -> T1: ...

    @overload
    def get(
        self, svc_type1: type[T1], svc_type2: type[T2], /
    ) -> tuple[T1, T2]: ...

    @overload
    def get(
        self, svc_type1: type[T1], svc_type2: type[T2], svc_type3: type[T3], /
    ) -> tuple[T1, T2, T3]: ...

    @overload
    def get(
        self,
        svc_type1: type[T1],
        svc_type2: type[T2],
        svc_type3: type[T3],
        svc_type4: type[T4],
        /,
    ) -> tuple[T1, T2, T3, T4]: ...

    @overload
    def get(
        self,
        svc_type1: type[T1],
        svc_type2: type[T2],
        svc_type3: type[T3],
        svc_type4: type[T4],
        svc_type5: type[T5],
        /,
    ) -> tuple[T1, T2, T3, T4, T5]: ...

    @overload
    def get(
        self,
        svc_type1: type[T1],
        svc_type2: type[T2],
        svc_type3: type[T3],
        svc_type4: type[T4],
        svc_type5: type[T5],
        svc_type6: type[T6],
        /,
    ) -> tuple[T1, T2, T3, T4, T5, T6]: ...

    @overload
    def get(
        self,
        svc_type1: type[T1],
        svc_type2: type[T2],
        svc_type3: type[T3],
        svc_type4: type[T4],
        svc_type5: type[T5],
        svc_type6: type[T6],
        svc_type7: type[T7],
        /,
    ) -> tuple[T1, T2, T3, T4, T5, T6, T7]: ...

    @overload
    def get(
        self,
        svc_type1: type[T1],
        svc_type2: type[T2],
        svc_type3: type[T3],
        svc_type4: type[T4],
        svc_type5: type[T5],
        svc_type6: type[T6],
        svc_type7: type[T7],
        svc_type8: type[T8],
        /,
    ) -> tuple[T1, T2, T3, T4, T5, T6, T7, T8]: ...

    @overload
    def get(
        self,
        svc_type1: type[T1],
        svc_type2: type[T2],
        svc_type3: type[T3],
        svc_type4: type[T4],
        svc_type5: type[T5],
        svc_type6: type[T6],
        svc_type7: type[T7],
        svc_type8: type[T8],
        svc_type9: type[T9],
        /,
    ) -> tuple[T1, T2, T3, T4, T5, T6, T7, T8, T9]: ...

    @overload
    def get(
        self,
        svc_type1: type[T1],
        svc_type2: type[T2],
        svc_type3: type[T3],
        svc_type4: type[T4],
        svc_type5: type[T5],
        svc_type6: type[T6],
        svc_type7: type[T7],
        svc_type8: type[T8],
        svc_type9: type[T9],
        svc_type10: type[T10],
        /,
    ) -> tuple[T1, T2, T3, T4, T5, T6, T7, T8, T9, T10]: ...

    def get(self, *svc_types: type) -> object:
        """
        Get services of *svc_types*.

        Instantiate them if necessary and register their cleanup.

        Returns:
             ``svc_types[0]`` | ``tuple[*svc_types]``: If one service is
             requested, it's returned directly. If multiple are requested, a
             tuple of services is returned.
        """
        rv = []
        for svc_type in svc_types:
            cached, svc, name, enter = self._lookup(svc_type)
            if cached:
                rv.append(svc)
                continue

            if iscoroutine(svc) or isinstance(
                svc, AbstractAsyncContextManager
            ):
                msg = "Use `aget()` for async factories."
                raise TypeError(msg)

            if enter and isinstance(svc, AbstractContextManager):
                self._on_close.append((name, svc))
                svc = svc.__enter__()

            self._instantiated[svc_type] = svc

            rv.append(svc)

        if len(rv) == 1:
            return rv[0]

        return rv

    @overload
    async def aget(self, svc_type: type[T1], /) -> T1: ...

    @overload
    async def aget(
        self, svc_type1: type[T1], svc_type2: type[T2], /
    ) -> tuple[T1, T2]: ...

    @overload
    async def aget(
        self, svc_type1: type[T1], svc_type2: type[T2], svc_type3: type[T3], /
    ) -> tuple[T1, T2, T3]: ...

    @overload
    async def aget(
        self,
        svc_type1: type[T1],
        svc_type2: type[T2],
        svc_type3: type[T3],
        svc_type4: type[T4],
        /,
    ) -> tuple[T1, T2, T3, T4]: ...

    @overload
    async def aget(
        self,
        svc_type1: type[T1],
        svc_type2: type[T2],
        svc_type3: type[T3],
        svc_type4: type[T4],
        svc_type5: type[T5],
        /,
    ) -> tuple[T1, T2, T3, T4, T5]: ...

    @overload
    async def aget(
        self,
        svc_type1: type[T1],
        svc_type2: type[T2],
        svc_type3: type[T3],
        svc_type4: type[T4],
        svc_type5: type[T5],
        svc_type6: type[T6],
        /,
    ) -> tuple[T1, T2, T3, T4, T5, T6]: ...

    @overload
    async def aget(
        self,
        svc_type1: type[T1],
        svc_type2: type[T2],
        svc_type3: type[T3],
        svc_type4: type[T4],
        svc_type5: type[T5],
        svc_type6: type[T6],
        svc_type7: type[T7],
        /,
    ) -> tuple[T1, T2, T3, T4, T5, T6, T7]: ...

    @overload
    async def aget(
        self,
        svc_type1: type[T1],
        svc_type2: type[T2],
        svc_type3: type[T3],
        svc_type4: type[T4],
        svc_type5: type[T5],
        svc_type6: type[T6],
        svc_type7: type[T7],
        svc_type8: type[T8],
        /,
    ) -> tuple[T1, T2, T3, T4, T5, T6, T7, T8]: ...

    @overload
    async def aget(
        self,
        svc_type1: type[T1],
        svc_type2: type[T2],
        svc_type3: type[T3],
        svc_type4: type[T4],
        svc_type5: type[T5],
        svc_type6: type[T6],
        svc_type7: type[T7],
        svc_type8: type[T8],
        svc_type9: type[T9],
        /,
    ) -> tuple[T1, T2, T3, T4, T5, T6, T7, T8, T9]: ...

    @overload
    async def aget(
        self,
        svc_type1: type[T1],
        svc_type2: type[T2],
        svc_type3: type[T3],
        svc_type4: type[T4],
        svc_type5: type[T5],
        svc_type6: type[T6],
        svc_type7: type[T7],
        svc_type8: type[T8],
        svc_type9: type[T9],
        svc_type10: type[T10],
        /,
    ) -> tuple[T1, T2, T3, T4, T5, T6, T7, T8, T9, T10]: ...

    async def aget(self, *svc_types: type) -> object:
        """
        Same as :meth:`get` but instantiates asynchronously, if necessary.

        Also works with synchronous services, so in an async application, just
        use this.

        .. versionchanged:: 24.2.0
           Synchronous context managers are now entered/exited, too.
        """
        rv = []
        for svc_type in svc_types:
            cached, svc, name, enter = self._lookup(svc_type)
            if cached:
                rv.append(svc)
                continue

            if enter and isinstance(svc, AbstractAsyncContextManager):
                self._on_close.append((name, svc))
                svc = await svc.__aenter__()
            elif enter and isinstance(svc, AbstractContextManager):
                self._on_close.append((name, svc))
                svc = svc.__enter__()
            elif isawaitable(svc):
                svc = await svc

            self._instantiated[svc_type] = svc

            rv.append(svc)

        if len(rv) == 1:
            return rv[0]

        return rv
