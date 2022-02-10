import uuid
from unittest.mock import Mock

from django.core.handlers.base import BaseHandler
from freezegun import freeze_time

from ..jwt import (
    JWT_REFRESH_TOKEN_COOKIE_NAME,
    JWT_REFRESH_TYPE,
    create_refresh_token,
    jwt_encode,
    jwt_user_payload,
)
from ..middleware import API_PATH


@freeze_time("2020-03-18 12:00:00")
def test_jwt_refresh_token_middleware(rf, customer_user, settings):
    refresh_token = create_refresh_token(customer_user)
    settings.MIDDLEWARE = [
        "saleor.core.middleware.jwt_refresh_token_middleware",
    ]
    request = rf.request()
    request.refresh_token = refresh_token
    handler = BaseHandler()
    handler.load_middleware()
    response = handler.get_response(request)
    cookie = response.cookies.get(JWT_REFRESH_TOKEN_COOKIE_NAME)
    assert cookie.value == refresh_token


@freeze_time("2020-03-18 12:00:00")
def test_jwt_refresh_token_middleware_token_without_expire(rf, customer_user, settings):
    settings.JWT_EXPIRE = True
    payload = jwt_user_payload(
        customer_user,
        JWT_REFRESH_TYPE,
        settings.JWT_TTL_REFRESH,
    )
    del payload["exp"]

    refresh_token = jwt_encode(payload)
    settings.MIDDLEWARE = [
        "saleor.core.middleware.jwt_refresh_token_middleware",
    ]
    request = rf.request()
    request.refresh_token = refresh_token
    handler = BaseHandler()
    handler.load_middleware()
    response = handler.get_response(request)
    cookie = response.cookies.get(JWT_REFRESH_TOKEN_COOKIE_NAME)
    assert cookie.value == refresh_token


@freeze_time("2020-03-18 12:00:00")
def test_jwt_refresh_token_middleware_samesite_debug_mode(rf, customer_user, settings):
    refresh_token = create_refresh_token(customer_user)
    settings.MIDDLEWARE = [
        "saleor.core.middleware.jwt_refresh_token_middleware",
    ]
    settings.DEBUG = True
    request = rf.request()
    request.refresh_token = refresh_token
    handler = BaseHandler()
    handler.load_middleware()
    response = handler.get_response(request)
    cookie = response.cookies.get(JWT_REFRESH_TOKEN_COOKIE_NAME)
    assert cookie["samesite"] == "Lax"


@freeze_time("2020-03-18 12:00:00")
def test_jwt_refresh_token_middleware_samesite_none(rf, customer_user, settings):
    refresh_token = create_refresh_token(customer_user)
    settings.MIDDLEWARE = [
        "saleor.core.middleware.jwt_refresh_token_middleware",
    ]
    settings.DEBUG = False
    request = rf.request()
    request.refresh_token = refresh_token
    handler = BaseHandler()
    handler.load_middleware()
    response = handler.get_response(request)
    cookie = response.cookies.get(JWT_REFRESH_TOKEN_COOKIE_NAME)
    assert cookie["samesite"] == "None"


def test_plugins_middleware_loads_requestor_in_plugin(rf, customer_user, settings):
    settings.MIDDLEWARE = [
        "saleor.core.middleware.plugins",
    ]
    settings.PLUGINS = ["saleor.plugins.tests.sample_plugins.ActivePlugin"]
    request = rf.request()
    request.user = customer_user
    request.app = None

    handler = BaseHandler()
    handler.load_middleware()
    handler.get_response(request)
    plugin = request.plugins.all_plugins.pop()

    assert isinstance(plugin.requestor, type(customer_user))
    assert plugin.requestor.id == customer_user.id


def test_plugins_middleware_requestor_in_plugin_when_no_app_and_user_in_req_is_none(
    rf, settings
):
    settings.MIDDLEWARE = [
        "saleor.core.middleware.plugins",
    ]
    settings.PLUGINS = ["saleor.plugins.tests.sample_plugins.ActivePlugin"]
    request = rf.request()
    request.user = None
    request.app = None

    handler = BaseHandler()
    handler.load_middleware()
    handler.get_response(request)
    plugin = request.plugins.all_plugins.pop()

    assert not plugin.requestor


def test_api_reporter_middleware_when_deactivated(rf, app, settings):
    settings.MIDDLEWARE = [
        "saleor.core.middleware.api_reporter",
    ]
    request = rf.post(API_PATH)
    request.app = app
    plugins = Mock(return_value=None)
    request.plugins = plugins

    handler = BaseHandler()
    handler.load_middleware()
    handler.get_response(request)

    plugins.report_api_call.assert_not_called()


def test_api_reporter_middleware_when_app_makes_graphql_req(rf, app, settings):
    settings.MIDDLEWARE = [
        "saleor.core.middleware.api_reporter",
    ]
    settings.REPORTER_ACTIVE = True
    request = rf.post(API_PATH)
    request.app = app
    plugins = Mock(return_value=None)
    request.plugins = plugins

    handler = BaseHandler()
    handler.load_middleware()
    response = handler.get_response(request)

    assert isinstance(request.request_uuid, uuid.UUID)
    plugins.report_api_call.assert_called_once_with(request, response)


def test_api_reporter_middleware_when_non_app_makes_graphql_req(rf, settings):
    settings.MIDDLEWARE = [
        "saleor.core.middleware.api_reporter",
    ]
    settings.REPORTER_ACTIVE = True
    request = rf.post(API_PATH)
    request.app = None
    plugins = Mock(return_value=None)
    request.plugins = plugins

    handler = BaseHandler()
    handler.load_middleware()
    handler.get_response(request)

    plugins.report_api_call.assert_not_called()
