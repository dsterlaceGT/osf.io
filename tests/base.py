# -*- coding: utf-8 -*-
'''Base TestCase class for OSF unittests. Uses a temporary MongoDB database.'''
import os
import re
import shutil
import logging
import unittest
import functools
import datetime as dt
from flask import g
from json import dumps

import blinker
import httpretty
from webtest_plus import TestApp
from webtest.utils import NoDefault

import mock
from faker import Factory
from nose.tools import *  # noqa (PEP8 asserts)
from pymongo.errors import OperationFailure
from modularodm import storage


from api.base.wsgi import application as django_app
from framework.mongo import set_up_storage
from framework.auth import User
from framework.sessions.model import Session
from framework.guid.model import Guid
from framework.mongo import client as client_proxy
from framework.mongo import database as database_proxy
from framework.transactions import commands, messages, utils

from website.project.model import (
    Node, NodeLog, Tag, WatchConfig,
)
from website import settings

from website.addons.wiki.model import NodeWikiPage

import website.models
from website.signals import ALL_SIGNALS
from website.project.signals import contributor_added
from website.app import init_app
from website.addons.base import AddonConfig
from website.project.views.contributor import notify_added_contributor

# Just a simple app without routing set up or backends
test_app = init_app(
    settings_module='website.settings', routes=True, set_backends=False,
)
test_app.testing = True


# Silence some 3rd-party logging and some "loud" internal loggers
SILENT_LOGGERS = [
    'factory.generate',
    'factory.containers',
    'website.search.elastic_search',
    'framework.auth.core',
    'website.mails',
    'website.search_migration.migrate',
    'website.util.paths',
]
for logger_name in SILENT_LOGGERS:
    logging.getLogger(logger_name).setLevel(logging.CRITICAL)

# Fake factory
fake = Factory.create()

# All Models
MODELS = (User, Node, NodeLog, NodeWikiPage,
          Tag, WatchConfig, Session, Guid)


def teardown_database(client=None, database=None):
    client = client or client_proxy
    database = database or database_proxy
    try:
        commands.rollback(database)
    except OperationFailure as error:
        message = utils.get_error_message(error)
        if messages.NO_TRANSACTION_ERROR not in message:
            raise
    client.drop_database(database)


class DbTestCase(unittest.TestCase):
    """Base `TestCase` for tests that require a scratch database.
    """
    DB_NAME = getattr(settings, 'TEST_DB_NAME', 'osf_test')

    # dict of addons to inject into the app.
    ADDONS_UNDER_TEST = {}
    # format: {
    #    <addon shortname>: {
    #        'user_settings': <AddonUserSettingsBase instance>,
    #        'node_settings': <AddonNodeSettingsBase instance>,
    #}

    # list of AddonConfig instances of injected addons
    __ADDONS_UNDER_TEST = []

    @classmethod
    def setUpClass(cls):
        super(DbTestCase, cls).setUpClass()

        for (short_name, options) in cls.ADDONS_UNDER_TEST.iteritems():
            cls.__ADDONS_UNDER_TEST.append(
                init_mock_addon(short_name, **options)
            )

        cls._original_db_name = settings.DB_NAME
        settings.DB_NAME = cls.DB_NAME
        cls._original_piwik_host = settings.PIWIK_HOST
        settings.PIWIK_HOST = None
        cls._original_enable_email_subscriptions = settings.ENABLE_EMAIL_SUBSCRIPTIONS
        settings.ENABLE_EMAIL_SUBSCRIPTIONS = False

        cls._original_bcrypt_log_rounds = settings.BCRYPT_LOG_ROUNDS
        settings.BCRYPT_LOG_ROUNDS = 1

        teardown_database(database=database_proxy._get_current_object())
        # TODO: With `database` as a `LocalProxy`, we should be able to simply
        # this logic
        set_up_storage(
            website.models.MODELS,
            storage.MongoStorage,
            addons=settings.ADDONS_AVAILABLE,
        )
        cls.db = database_proxy

    @classmethod
    def tearDownClass(cls):
        super(DbTestCase, cls).tearDownClass()

        for addon in cls.__ADDONS_UNDER_TEST:
            remove_mock_addon(addon)

        teardown_database(database=database_proxy._get_current_object())
        settings.DB_NAME = cls._original_db_name
        settings.PIWIK_HOST = cls._original_piwik_host
        settings.ENABLE_EMAIL_SUBSCRIPTIONS = cls._original_enable_email_subscriptions
        settings.BCRYPT_LOG_ROUNDS = cls._original_bcrypt_log_rounds


class AppTestCase(unittest.TestCase):
    """Base `TestCase` for OSF tests that require the WSGI app (but no database).
    """

    DISCONNECTED_SIGNALS = {
        # disconnect notify_add_contributor so that add_contributor does not send "fake" emails in tests
        contributor_added: [notify_added_contributor]
    }

    def setUp(self):
        super(AppTestCase, self).setUp()
        self.app = TestApp(test_app)
        self.context = test_app.test_request_context()
        self.context.push()
        with self.context:
            g._celery_tasks = []
        for signal in self.DISCONNECTED_SIGNALS:
            for receiver in self.DISCONNECTED_SIGNALS[signal]:
                signal.disconnect(receiver)

    def tearDown(self):
        super(AppTestCase, self).tearDown()
        with mock.patch('website.mailchimp_utils.get_mailchimp_api'):
            self.context.pop()
        for signal in self.DISCONNECTED_SIGNALS:
            for receiver in self.DISCONNECTED_SIGNALS[signal]:
                signal.connect(receiver)


class ApiAppTestCase(unittest.TestCase):
    """Base `TestCase` for OSF API tests that require the WSGI app (but no database).
    """

    def setUp(self):
        super(ApiAppTestCase, self).setUp()
        self.app = TestAppJSONAPI(django_app)


class TestAppJSONAPI(TestApp):
    """
    Extends TestApp to add json_api_methods(post, put, patch, and delete)
    which put content_type 'application/vnd.api+json' in header. Adheres to
    JSON API spec.
    """

    def __init__(self, app, *args, **kwargs):
        super(TestAppJSONAPI, self).__init__(app, *args, **kwargs)
        self.auth = None
        self.auth_type = 'basic'

    def json_api_method(method):

        def wrapper(self, url, params=NoDefault, **kw):
            content_type = 'application/vnd.api+json'
            if params is not NoDefault:
                params = dumps(params, cls=self.JSONEncoder)
            kw.update(
                params=params,
                content_type=content_type,
                upload_files=None,
            )
            return self._gen_request(method, url, **kw)

        subst = dict(lmethod=method.lower(), method=method)
        wrapper.__name__ = str('%(lmethod)s_json_api' % subst)

        return wrapper

    post_json_api = json_api_method('POST')
    put_json_api = json_api_method('PUT')
    patch_json_api = json_api_method('PATCH')
    delete_json_api = json_api_method('DELETE')


class UploadTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """Store uploads in temp directory.
        """
        super(UploadTestCase, cls).setUpClass()
        cls._old_uploads_path = settings.UPLOADS_PATH
        cls._uploads_path = os.path.join('/tmp', 'osf', 'uploads')
        try:
            os.makedirs(cls._uploads_path)
        except OSError:  # Path already exists
            pass
        settings.UPLOADS_PATH = cls._uploads_path

    @classmethod
    def tearDownClass(cls):
        """Restore uploads path.
        """
        super(UploadTestCase, cls).tearDownClass()
        shutil.rmtree(cls._uploads_path)
        settings.UPLOADS_PATH = cls._old_uploads_path


methods = [
    httpretty.GET,
    httpretty.PUT,
    httpretty.HEAD,
    httpretty.POST,
    httpretty.PATCH,
    httpretty.DELETE,
]
def kill(*args, **kwargs):
    raise httpretty.errors.UnmockedError


class MockRequestTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        super(MockRequestTestCase, cls).setUpClass()
        httpretty.enable()
        for method in methods:
            httpretty.register_uri(
                method,
                re.compile(r'.*'),
                body=kill,
                priority=-1,
            )

    def tearDown(self):
        super(MockRequestTestCase, self).tearDown()
        httpretty.reset()

    @classmethod
    def tearDownClass(cls):
        super(MockRequestTestCase, cls).tearDownClass()
        httpretty.reset()
        httpretty.disable()


class OsfTestCase(DbTestCase, AppTestCase, UploadTestCase, MockRequestTestCase):
    """Base `TestCase` for tests that require both scratch databases and the OSF
    application. Note: superclasses must call `super` in order for all setup and
    teardown methods to be called correctly.
    """
    pass


class ApiTestCase(DbTestCase, ApiAppTestCase, UploadTestCase, MockRequestTestCase):
    """Base `TestCase` for tests that require both scratch databases and the OSF
    API application. Note: superclasses must call `super` in order for all setup and
    teardown methods to be called correctly.
    """
    def setUp(self):
        super(ApiTestCase, self).setUp()
        settings.USE_EMAIL = False
        

# From Flask-Security: https://github.com/mattupstate/flask-security/blob/develop/flask_security/utils.py
class CaptureSignals(object):
    """Testing utility for capturing blinker signals.

    Context manager which mocks out selected signals and registers which
    are `sent` on and what arguments were sent. Instantiate with a list of
    blinker `NamedSignals` to patch. Each signal has its `send` mocked out.

    """
    def __init__(self, signals):
        """Patch all given signals and make them available as attributes.

        :param signals: list of signals

        """
        self._records = {}
        self._receivers = {}
        for signal in signals:
            self._records[signal] = []
            self._receivers[signal] = functools.partial(self._record, signal)

    def __getitem__(self, signal):
        """All captured signals are available via `ctxt[signal]`.
        """
        if isinstance(signal, blinker.base.NamedSignal):
            return self._records[signal]
        else:
            super(CaptureSignals, self).__setitem__(signal)

    def _record(self, signal, *args, **kwargs):
        self._records[signal].append((args, kwargs))

    def __enter__(self):
        for signal, receiver in self._receivers.items():
            signal.connect(receiver)
        return self

    def __exit__(self, type, value, traceback):
        for signal, receiver in self._receivers.items():
            signal.disconnect(receiver)

    def signals_sent(self):
        """Return a set of the signals sent.
        :rtype: list of blinker `NamedSignals`.

        """
        return set([signal for signal, _ in self._records.items() if self._records[signal]])


def capture_signals():
    """Factory method that creates a ``CaptureSignals`` with all OSF signals."""
    return CaptureSignals(ALL_SIGNALS)


def assert_is_redirect(response, msg="Response is a redirect."):
    assert 300 <= response.status_code < 400, msg


def assert_before(lst, item1, item2):
    """Assert that item1 appears before item2 in lst."""
    assert_less(lst.index(item1), lst.index(item2),
        '{0!r} appears before {1!r}'.format(item1, item2))


def assert_datetime_equal(dt1, dt2, allowance=500):
    """Assert that two datetimes are about equal."""
    assert_less(dt1 - dt2, dt.timedelta(milliseconds=allowance))


def init_mock_addon(short_name, user_settings=None, node_settings=None):
    """Add an addon to the settings, so that it is ready for app init

    This is used to inject addons into the application context for tests."""

    #Importing within the function to prevent circular import problems.
    import factories
    user_settings = user_settings or factories.MockAddonUserSettings
    node_settings = node_settings or factories.MockAddonNodeSettings
    settings.ADDONS_REQUESTED.append(short_name)

    addon_config = AddonConfig(
        short_name=short_name,
        full_name=short_name,
        owners=['User', 'Node'],
        categories=['Storage'],
        user_settings_model=user_settings,
        node_settings_model=node_settings,
        models=[user_settings, node_settings],
    )
    settings.ADDONS_AVAILABLE_DICT[addon_config.short_name] = addon_config
    settings.ADDONS_AVAILABLE.append(addon_config)
    return addon_config


def remove_mock_addon(addon_config):
    """Given an AddonConfig instance, remove that addon from the settings"""
    settings.ADDONS_AVAILABLE_DICT.pop(addon_config.short_name, None)

    try:
        settings.ADDONS_AVAILABLE.remove(addon_config)
    except ValueError:
        pass

    try:
        settings.ADDONS_REQUESTED.remove(addon_config.short_name)
    except ValueError:
        pass
