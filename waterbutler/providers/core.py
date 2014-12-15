import os
import abc
import asyncio
import logging
import itertools

import furl
import aiohttp

from waterbutler.providers import exceptions

logger = logging.getLogger(__name__)

PROVIDERS = {}


def register_provider(name):
    """A decorator that adds the specifed class into the `PROVIDERS` dict
    :param str name: The name to register
    """
    def _register_provider(cls):
        if PROVIDERS.get(name):
            logging.warning('{} is already a registered provider'.format(name))

        PROVIDERS[name] = cls
        return cls
    return _register_provider


def get_provider(name):
    """Return the provider *class* of the registed name
    Raises a NotImplementedError if one is not found
    :param str name: Name of the provider to find
    :rtype type(BaseProvider):
    """
    try:
        return PROVIDERS[name]
    except KeyError:
        raise NotImplementedError('No provider for {}'.format(name))


def make_provider(name, auth=None, identity=None):
    """Fetches a provider registed under name and returns an instance of it
    :param str name: Name of the provider
    :param dict credentials: a dictionary containing keys `auth` and `identity`
    :rtype BaseProvider:
    """
    return get_provider(name)(auth or {}, identity or {})


def build_url(base, *segments, **query):
    url = furl.furl(base)
    segments = filter(
        lambda segment: segment,
        map(
            lambda segment: segment.strip('/'),
            itertools.chain(url.path.segments, segments)
        )
    )
    url.path = os.path.join(*segments)
    url.args = query
    return url.url


class BaseProvider(metaclass=abc.ABCMeta):

    BASE_URL = None

    def __init__(self, auth, identity):
        self.auth = auth
        self.identity = identity

    def __eq__(self, other):
        try:
            return (
                type(self) == type(other) and
                self.identity == other.identity
            )
        except AttributeError:
            return False

    def build_url(self, *segments, **query):
        return build_url(self.BASE_URL, *segments, **query)

    @property
    def default_headers(self):
        return {}

    def build_headers(self, **kwargs):
        headers = self.default_headers
        headers.update(kwargs)
        return {
            key: value
            for key, value in headers.items()
            if value is not None
        }

    @asyncio.coroutine
    def make_request(self, *args, **kwargs):
        kwargs['headers'] = self.build_headers(**kwargs.get('headers', {}))
        expects = kwargs.pop('expects', None)
        throws = kwargs.pop('throws', exceptions.ProviderError)
        response = yield from aiohttp.request(*args, **kwargs)
        if expects and response.status not in expects:
            raise (yield from exceptions.exception_from_response(response, error=throws, **kwargs))
        return response

    def can_intra_copy(self, other):
        return False

    def can_intra_move(self, other):
        return False

    def intra_copy(self, dest_provider, source_options, dest_options):
        raise NotImplementedError

    @asyncio.coroutine
    def intra_move(self, dest_provider, source_options, dest_options):
        resp = yield from self.intra_copy(dest_provider, source_options, dest_options)
        yield from self.delete(**source_options)
        return resp

    @asyncio.coroutine
    def copy(self, dest_provider, source_options, dest_options):
        if self.can_intra_copy(dest_provider):
            try:
                return (yield from self.intra_copy(dest_provider, source_options, dest_options))
            except NotImplementedError:
                pass
        stream = yield from self.download(**source_options)
        yield from dest_provider.upload(stream, **dest_options)

    @asyncio.coroutine
    def move(self, dest_provider, source_options, dest_options):
        if self.can_intra_move(dest_provider):
            try:
                return (yield from self.intra_move(dest_provider, source_options, dest_options))
            except NotImplementedError:
                pass
        yield from self.copy(dest_provider, source_options, dest_options)
        yield from self.delete(**source_options)

    @abc.abstractmethod
    def download(self, **kwargs):
        pass

    @abc.abstractmethod
    def upload(self, stream, **kwargs):
        pass

    @abc.abstractmethod
    def delete(self, **kwargs):
        pass

    @abc.abstractmethod
    def metadata(self, **kwargs):
        pass


class BaseMetadata(metaclass=abc.ABCMeta):

    def __init__(self, raw):
        self.raw = raw

    def serialized(self):
        return {
            'provider': self.provider,
            'kind': self.kind,
            'name': self.name,
            'size': self.size,
            'path': self.path,
            'modified': self.modified,
            'extra': self.extra,
        }

    @abc.abstractproperty
    def provider(self):
        pass

    @abc.abstractproperty
    def kind(self):
        pass

    @abc.abstractproperty
    def name(self):
        pass

    @abc.abstractproperty
    def path(self):
        pass

    @abc.abstractproperty
    def modified(self):
        pass

    @abc.abstractproperty
    def size(self):
        pass

    @property
    def extra(self):
        return {}