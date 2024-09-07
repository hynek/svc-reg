# SPDX-FileCopyrightText: 2023 Hynek Schlawack <hs@ox.cx>
#
# SPDX-License-Identifier: MIT

import contextlib
import gc
import importlib.util
import inspect
import logging
import sys

from unittest.mock import AsyncMock, Mock

import pytest

import svcs

from .fake_factories import async_str_gen_factory, str_gen_factory
from .helpers import nop
from .ifaces import AnotherService, Interface, Service, YetAnotherService


needs_working_async_mock = pytest.mark.skipif(
    not inspect.iscoroutinefunction(AsyncMock()),
    reason="AsyncMock not working",
)


@pytest.fixture(name="create_module")
def _create_module(tmp_path):
    def wrapper(source):
        module_name = "_svcs_testing_tmp_module"
        module_path = tmp_path / f"{module_name}.py"
        module_path.write_text(source)

        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        return module

    return wrapper


class TestRegistry:
    def test_repr_empty(self, registry):
        """
        repr of an empty registry says 0 registered services.
        """
        assert "<svcs.Registry(num_services=0)>" == repr(registry)

    def test_iter_empty(self, registry):
        """
        Iterating over an empty registry returns nothing.
        """
        assert not list(registry)

    def test_repr_counts(self, registry):
        """
        repr counts 2 registered services as 2.
        """
        registry.register_factory(Service, Service)
        registry.register_factory(AnotherService, AnotherService)

        assert "<svcs.Registry(num_services=2)>" == repr(registry)

    def test_generators_become_context_managers(self, registry):
        """
        If a generator-based factory is passed, it's automatically wrapped with
        @contextmanager.
        """
        registry.register_factory(Service, str_gen_factory)
        cm = registry.get_registered_service_for(Service).factory

        with cm():
            ...

    @pytest.mark.asyncio
    async def test_async_generators_become_context_managers(self, registry):
        """
        If a generator-based factory is passed, it's automatically wrapped with
        @contextmanager.
        """
        registry.register_factory(Service, async_str_gen_factory)
        cm = registry.get_registered_service_for(Service).factory

        async with cm():
            ...

    def test_empty_close(self):
        """
        Closing an empty registry does nothing.
        """
        svcs.Registry().close()

        with svcs.Registry():
            ...

    def test_close_closes(self, registry):
        """
        Calling close on Registry runs all on_close callbacks.
        """
        close_1 = Mock()
        close_2 = Mock()

        registry.register_factory(Service, Service, on_registry_close=close_1)
        registry.register_value(
            AnotherService, AnotherService, on_registry_close=close_2
        )

        registry.close()

        assert close_1.called
        assert close_2.called
        assert not registry._services
        assert not registry._on_close

    def test_overwritten_factories_are_not_forgotten(self, registry):
        """
        If a factory is overwritten, it's close callback is still called.
        """
        close_1 = Mock()
        close_2 = Mock()

        registry.register_factory(Service, Service, on_registry_close=close_1)
        registry.register_value(
            Service, AnotherService, on_registry_close=close_2
        )

        registry.close()

        assert close_1.called
        assert close_2.called

    def test_close_warns_about_async(self, registry):
        """
        Calling close raises a warning if there are async cleanups.
        """

        async def callback(): ...

        registry.register_factory(Service, Service, on_registry_close=callback)

        with pytest.warns(
            UserWarning,
            match="Skipped async cleanup for 'tests.ifaces.Service'.",
        ):
            registry.close()

    def test_register_factory_logs(self, registry, caplog):
        """
        register_factory logs the registration to debug.
        """
        caplog.set_level(logging.DEBUG)

        registry.register_factory(Interface, Service)

        assert [
            "registered factory <class 'tests.ifaces.Service'> for "
            "service type tests.ifaces.Interface"
        ] == caplog.messages
        assert "tests.ifaces.Interface" == caplog.records[0].svcs_service_name
        assert "tests.ifaces.Service" == caplog.records[0].svcs_factory_name

    def test_register_value_logs(self, registry, caplog):
        """
        register_value logs the registration to debug.
        """
        caplog.set_level(logging.DEBUG)

        registry.register_value(Service, 42)

        assert [
            "registered value 42 for service type tests.ifaces.Service"
        ] == caplog.messages
        assert "tests.ifaces.Service" == caplog.records[0].svcs_service_name
        assert 42 == caplog.records[0].svcs_value

    def test_close_logs_failures(self, registry, caplog):
        """
        Closing failures are logged but ignored.
        """
        registry.register_factory(
            Service, Service, on_registry_close=Mock(side_effect=ValueError())
        )

        with registry:
            ...

        assert [
            "Registry's on_registry_close callback failed for 'tests.ifaces.Service'."
        ] == caplog.messages
        assert "tests.ifaces.Service" == caplog.records[0].svcs_service_name

    def test_context_manager(self):
        """
        The registry is also a context manager that closes on exit.
        """
        orc = Mock()

        with svcs.Registry() as registry:
            registry.register_factory(Service, Service, on_registry_close=orc)

        orc.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_async_context_manager(self, close_me):
        """
        The registry is also an async context manager that acloses on exit.

        Passing an awaitable to on_registry_close works.
        """

        async def closer():
            close_me.close()

        async with svcs.Registry() as registry:
            registry.register_factory(
                Service, Service, on_registry_close=closer()
            )

        assert close_me.is_closed

    @pytest.mark.skipif(
        not hasattr(contextlib, "aclosing"),
        reason="Hasn't contextlib.aclosing()",
    )
    @pytest.mark.asyncio
    async def test_async_empty_close(self, registry):
        """
        Asynchronously closing an empty registry does nothing.
        """
        await registry.aclose()

        async with svcs.Registry():
            ...

    @pytest.mark.asyncio
    @needs_working_async_mock
    async def test_aclose_mixed(self, registry):
        """
        aclose() closes all services, including async ones.
        """
        sync_close = Mock()
        async_close = AsyncMock()

        registry.register_factory(
            Service, Service, on_registry_close=sync_close
        )
        registry.register_factory(
            AnotherService, AnotherService, on_registry_close=async_close
        )

        await registry.aclose()

        assert sync_close.called

        async_close.assert_awaited_once()

    @pytest.mark.asyncio
    @needs_working_async_mock
    async def test_aclose_logs_failures(self, registry, caplog):
        """
        Async closing failures are logged but ignored.
        """
        close_mock = AsyncMock(side_effect=ValueError())

        registry.register_factory(
            Service,
            Service,
            on_registry_close=close_mock,
        )

        await registry.aclose()

        close_mock.assert_awaited_once()
        assert "tests.ifaces.Service" == caplog.records[0].svcs_service_name

    def test_contains(self, registry):
        """
        If a service is registered with a registry, `in` returns True, False
        otherwise.
        """
        registry.register_factory(Service, Service)

        assert Service in registry
        assert AnotherService not in registry

    def test_iter(self, registry):
        """
        Iterating over a registry returns all registered services types.
        """
        registry.register_factory(Service, Service)
        registry.register_factory(AnotherService, AnotherService)
        registry.register_value(YetAnotherService, YetAnotherService)

        assert {Service, AnotherService, YetAnotherService} == set(registry)

    def test_gc_warning(self, recwarn):
        """
        If a registry is gc'ed with pending cleanups, a warning is raised.
        """

        def scope():
            registry = svcs.Registry()
            registry.register_value(int, 42, on_registry_close=nop)

        scope()

        gc.collect()

        assert (
            "Registry was garbage-collected with pending cleanups.",
        ) == recwarn.list[0].message.args


class TestRegisteredService:
    def test_repr(self, rs):
        """
        repr uses the fully-qualified name of a svc type.
        """

        assert (
            "<RegisteredService(svc_type=tests.ifaces.Service, "
            "factory=<class 'tests.ifaces.Service'>, "
            "takes_container=False, "
            "enter=True, "
            "has_ping=False"
            ")>"
        ) == repr(rs)

    def test_name(self, rs):
        """
        The name property deducts the correct class name.
        """

        assert "tests.ifaces.Service" == rs.name


def wrong_annotation(foo: svcs.Registry) -> int: ...


def no_args(): ...


def diff_name(): ...


takes_containers_annotation_string_modules = (
    """
from __future__ import annotations

from svcs import Container

def factory(container: Container) -> int:
    ...
    """,
    """
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from svcs import Container

def factory(container: Container) -> int:
    ...
    """,
    """
def factory(container: "svcs.Container") -> int:
    ...
    """,
    """
from __future__ import annotations

import svcs

def factory(container: svcs.Container) -> int:
    ...

    """,
    """
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import svcs

def factory(container: svcs.Container) -> int:
    ...
    """,
)


class TestTakesContainer:
    @pytest.mark.parametrize(
        "factory",
        [no_args, diff_name, wrong_annotation],
    )
    def test_nope(self, factory):
        """
        Functions with different names and annotations are ignored.
        """
        assert not svcs._core._takes_container(factory)

    def test_name(self):
        """
        Return True if the name is `svcs_container`.
        """

        def factory(svcs_container): ...

        assert svcs._core._takes_container(factory)

    def test_annotation(self):
        """
        Return true if the first argument is annotated as `svcs.Container`.
        """

        def factory(foo: svcs.Container): ...

        assert svcs._core._takes_container(factory)

    @pytest.mark.parametrize(
        "module_source", takes_containers_annotation_string_modules
    )
    def test_annotation_str(self, module_source, create_module):
        """
        Return `True` if the first argument is annotated as `svcs.Container`
        using a string.
        """

        module = create_module(module_source)

        assert svcs._core._takes_container(module.factory)

    def test_catches_invalid_sigs(self):
        """
        If the factory takes more than one parameter, raise an TypeError.
        """

        def factory(foo, bar): ...

        with pytest.raises(
            TypeError, match="Factories must take 0 or 1 parameters."
        ):
            svcs._core._takes_container(factory)

    def test_call_works(self):
        """
        Does not raise if the factory is a class with __call__.
        """

        class Factory:
            def __call__(self, svcs_container): ...

        assert svcs._core._takes_container(Factory())

    def test_signature_fails(self):
        """
        If getting the signature fails, it means the factory doesn't get a
        container.
        """
        assert not svcs._core._takes_container(int)


class TestFullName:
    def test_object(self):
        """
        Object has neither ``__module__`` nor ``__qualname__``, so it gets
        repr'ed.
        """
        assert svcs._core._full_name(object()).startswith(
            "<object object at 0x"
        )
