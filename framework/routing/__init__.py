# -*- coding: utf-8 -*-
import os
import logging
import copy
import json
import pystache
import httplib as http

import lxml.html
import werkzeug.wrappers
from werkzeug.exceptions import NotFound
from mako.template import Template
from mako.lookup import TemplateLookup

from framework import session, request, make_response
from framework.exceptions import HTTPError
from framework.flask import app, redirect
from website import settings

logger = logging.getLogger(__name__)

TEMPLATE_DIR = settings.TEMPLATES_PATH
_tpl_lookup = TemplateLookup(
    directories=[
        TEMPLATE_DIR,
        os.path.join(settings.BASE_PATH, 'addons/'),
    ],
    module_directory='/tmp/mako_modules'
)
REDIRECT_CODES = [
    http.MOVED_PERMANENTLY,
    http.FOUND,
]

class Rule(object):
    """ Container for routing and rendering rules."""

    @staticmethod
    def _ensure_list(value):
        if not isinstance(value, list):
            return [value]
        return value

    @staticmethod
    def _ensure_slash(value):
        if not value.endswith('/'):
            return value + '/'
        return value

    def __init__(self, routes, methods, view_func_or_data, renderer,
                 view_kwargs=None, endpoint_suffix=''):
        """Rule constructor.

        :param routes: Route or list of routes
        :param methods: HTTP method or list of methods
        :param view_func_or_data: View function or data; pass data
            if view returns a constant data dictionary
        :param renderer: Renderer object or function
        :param view_kwargs: Optional kwargs to pass to view function
        :param endpoint_suffix: Optional suffix to append to endpoint name;
            useful for disambiguating routes by HTTP verb

        """
        if not callable(renderer):
            raise ValueError('Argument renderer must be callable.')
        self.routes = [
            self._ensure_slash(route)
            for route in self._ensure_list(routes)
        ]
        self.methods = self._ensure_list(methods)
        self.view_func_or_data = view_func_or_data
        self.renderer = renderer
        self.view_kwargs = view_kwargs or {}
        self.endpoint_suffix = endpoint_suffix


def wrap_with_renderer(fn, renderer, renderer_kwargs=None, debug_mode=True):
    """

    :param fn: View function; must return a dictionary or a tuple containing
        (up to) a dictionary, status code, headers, and redirect URL
    :param renderer: Renderer object or function
    :param renderer_kwargs: Optional kwargs to pass to renderer
    :return: Wrapped view function

    """
    def wrapped(*args, **kwargs):
        session_error_code = session.data.get('auth_error_code')
        if session_error_code:
            raise HTTPError(session_error_code)
        try:
            if renderer_kwargs:
                kwargs.update(renderer_kwargs)
            data = fn(*args, **kwargs)
        except HTTPError as error:
            data = error
        except Exception as error:
            logger.debug('Exception raised in wrap_with_renderer')
            logger.error(error)
            if debug_mode:
                raise
            data = HTTPError(
                http.INTERNAL_SERVER_ERROR,
                message=repr(error),
            )
        return renderer(data, **renderer_kwargs or {})
    return wrapped


def data_to_lambda(data):
    """Create a lambda function that takes arbitrary arguments and returns
    a deep copy of the passed data. This function must deep copy the data,
    else other code operating on the returned data can change the return value
    of the lambda.

    """
    return lambda *args, **kwargs: copy.deepcopy(data)


view_functions = {}

def process_rules(app, rules, prefix=''):
    """Add URL routes to Flask / Werkzeug lookup table.

    :param app: Flask / Werkzeug app
    :param rules: List of Rule objects
    :param prefix: Optional prefix for rule URLs

    """
    for rule in rules:

        # Handle view function
        if callable(rule.view_func_or_data):

            view_func = rule.view_func_or_data
            renderer_name = getattr(
                rule.renderer,
                '__name__',
                rule.renderer.__class__.__name__
            )
            endpoint = '{}__{}'.format(
                renderer_name,
                rule.view_func_or_data.__name__
            )
            view_functions[endpoint] = rule.view_func_or_data

        # Handle view data: wrap in lambda and build endpoint from
        # HTTP methods
        else:

            view_func = data_to_lambda(rule.view_func_or_data)
            endpoint = '__'.join(
                route.replace('/', '') for route in rule.routes
            )

        # Wrap view function with renderer
        wrapped_view_func = wrap_with_renderer(
            view_func,
            rule.renderer,
            rule.view_kwargs,
            debug_mode=app.debug
        )

        # Add routes
        for url in rule.routes:
            app.add_url_rule(
                prefix + url,
                endpoint=endpoint + rule.endpoint_suffix,
                view_func=wrapped_view_func,
                methods=rule.methods,
            )


### Renderer helpers ###

def render_mustache_string(tpl_string, data):
    return pystache.render(tpl_string, context=data)

def render_jinja_string(tpl, data):
    pass

mako_cache = {}
def render_mako_string(tpldir, tplname, data):

    tpl = mako_cache.get(tplname)
    if tpl is None:
        tpl = Template(
            open(os.path.join(tpldir, tplname)).read(),
            lookup=_tpl_lookup,
            input_encoding='utf-8',
            output_encoding='utf-8',
        )
    # Don't cache in debug mode
    if not app.debug:
        mako_cache[tplname] = tpl
    return tpl.render(**data)

renderer_extension_map = {
    '.stache': render_mustache_string,
    '.jinja': render_jinja_string,
    '.mako': render_mako_string,
}

def unpack(data, n=4):
    """Unpack data to tuple of length n.

    :param data: Object or tuple of length <= n
    :param n: Length to pad tuple

    """
    if not isinstance(data, tuple):
        data = (data,)
    return data + (None,) * (n - len(data))


def proxy_url(url):
    """Call Flask view function for a given URL.

    :param url: URL to follow
    :return: Return value of view function, wrapped in Werkzeug Response

    """
    # Get URL map, passing current request method; else method defaults to GET
    match = app.url_map.bind('').match(url, method=request.method)
    response = app.view_functions[match[0]](**match[1])
    return make_response(response)


def call_url(url, view_kwargs=None):
    """Look up and call view function by URL.

    :param url: URL
    :param view_kwargs: Optional kwargs to pass to view function
    :return: Data from view function

    """
    # Parse view function and args
    func_name, func_data = app.url_map.bind('').match(url)
    if view_kwargs is not None:
        func_data.update(view_kwargs)
    view_function = view_functions[func_name]

    # Call view function
    rv = view_function(**func_data)

    # Extract data from return value
    rv, _, _, _ = unpack(rv)

    # Follow redirects
    if isinstance(rv, werkzeug.wrappers.BaseResponse) \
            and rv.status_code in REDIRECT_CODES:
        redirect_url = rv.headers['Location']
        return call_url(redirect_url)

    return rv

### Renderers ###

class Renderer(object):

    CONTENT_TYPE = 'text/html'

    def render(self, data, redirect_url, *args, **kwargs):
        raise NotImplementedError

    def handle_error(self, error):
        raise NotImplementedError

    def __call__(self, data, *args, **kwargs):
        """Render data returned by a view function.

        :param data: Dictionary or tuple of (up to) dictionary,
            status code, headers, and redirect URL
        :return: Flask / Werkzeug response object

        """
        # Handle error
        if isinstance(data, HTTPError):
            return self.handle_error(data)

        # Return if response
        if isinstance(data, werkzeug.wrappers.BaseResponse):
            return data

        # Unpack tuple
        data, status_code, headers, redirect_url = unpack(data)

        # Call subclass render
        rendered = self.render(data, redirect_url, *args, **kwargs)

        # Return if response
        if isinstance(rendered, werkzeug.wrappers.BaseResponse):
            return rendered

        # Set content type in headers
        headers = headers or {}
        headers["Content-Type"] = self.CONTENT_TYPE + "; charset=" + kwargs.get("charset", "utf-8")

        # Package as response
        return make_response(rendered, status_code, headers)


class JSONRenderer(Renderer):
    """Renderer for API views. Generates JSON; ignores
    redirects from views and exceptions.

    """

    CONTENT_TYPE = "application/json"

    class Encoder(json.JSONEncoder):
        def default(self, obj):
            if hasattr(obj, 'to_json'):
                try:
                    return obj.to_json()
                except TypeError:  # BS4 objects have to_json that isn't callable
                    return unicode(obj)
            return json.JSONEncoder.default(self, obj)

    def handle_error(self, error):
        return self.render(error.to_data(), None), error.code

    def render(self, data, redirect_url, *args, **kwargs):
        return json.dumps(data, cls=self.Encoder)

# Create a single JSONRenderer instance to avoid repeated construction
json_renderer = JSONRenderer()

class WebRenderer(Renderer):
    """Renderer for web views. Generates HTML; follows redirects
    from views and exceptions.

    """

    CONTENT_TYPE = 'text/html'
    error_template = 'error.mako'

    def detect_renderer(self, renderer, filename):

        if renderer:
            return renderer

        try:
            _, extension = os.path.splitext(filename)
            return renderer_extension_map[extension]
        except KeyError:
            raise KeyError(
                'Could not infer renderer from file name: {}'.format(
                    filename
                )
            )

    def __init__(self, template_name, renderer=None, error_renderer=None,
                 data=None, detect_render_nested=True,
                 template_dir=TEMPLATE_DIR):
        """Construct WebRenderer.

        :param template_name: Name of template file
        :param renderer: Renderer callable; attempt to auto-detect if None
        :param error_renderer: Renderer for error views; attempt to
            auto-detect if None
        :param data: Optional dictionary or dictionary-generating function
                     to add to data from view function
        :param detect_render_nested: Auto-detect renderers for nested
            templates?
        :param template_dir: Path to template directory

        """
        self.template_name = template_name
        self.data = data or {}
        self.detect_render_nested = detect_render_nested
        self.template_dir = template_dir

        self.renderer = self.detect_renderer(renderer, template_name)
        self.error_renderer = self.detect_renderer(
            error_renderer,
            self.error_template
        )

    def handle_error(self, error):
        """Handle an HTTPError.

        :param error: HTTPError object
        :return: HTML error page
        """

        # Follow redirects
        if error.redirect_url is not None:
            return redirect(error.redirect_url)

        # Render error page
        # todo: use message / data from exception in error page
        error_data = error.to_data()
        return self.render(
            error_data,
            None,
            template_name=self.error_template
        ), error.code

    def render_element(self, element, data):
        """Render an embedded template.

        :param element: The template embed (HtmlElement).
             Ex: <div mod-meta='{"tpl": "name.html", "replace": true}'></div>
        :param data: Dictionary to be passed to the template as context
        :return: 2-tuple: (<result>, <flag: replace div>)
        """
        attributes_string = element.get("mod-meta")

        # Return debug <div> if JSON cannot be parsed
        try:
            element_meta = json.loads(attributes_string)
        except ValueError:
            return '<div>No JSON object could be decoded: {}</div>'.format(
                attributes_string
            ), True

        uri = element_meta.get('uri')
        is_replace = element_meta.get('replace', False)
        kwargs = element_meta.get('kwargs', {})
        view_kwargs = element_meta.get('view_kwargs', {})
        error_msg = element_meta.get('error', None)

        # TODO: Is copy enough? Discuss.
        render_data = copy.copy(data)
        render_data.update(kwargs)

        if uri:
            # Catch errors and return appropriate debug divs
            # todo: add debug parameter
            try:
                uri_data = call_url(uri, view_kwargs=view_kwargs)
                render_data.update(uri_data)
            except NotFound:
                return '<div>URI {} not found</div>'.format(uri), is_replace
            except Exception as error:
                if error_msg:
                    return '<div>{}</div>'.format(error_msg), is_replace
                return '<div>Error retrieving URI {}: {}</div>'.format(
                    uri,
                    repr(error)
                ), is_replace

        try:
            template_rendered = self._render(
                render_data,
                element_meta['tpl'],
            )
        except Exception as error:
            return '<div>Error rendering template {}: {}'.format(
                element_meta['tpl'],
                repr(error)
            ), is_replace

        return template_rendered, is_replace

    def _render(self, data, template_name=None):
        """Render output of view function to HTML.

        :param data: Data dictionary from view function
        :param template_name: Name of template file
        :return: Rendered HTML
        """

        nested = template_name is None
        template_name = template_name or self.template_name

        if nested and self.detect_render_nested:
            try:
                renderer = self.detect_renderer(None, template_name)
            except KeyError:
                renderer = self.renderer
        else:
            renderer = self.renderer

        # Catch errors and return appropriate debug divs
        # todo: add debug parameter
        try:
            rendered = renderer(self.template_dir, template_name, data)
        except IOError:
            return '<div>Template {} not found.</div>'.format(template_name)

        html = lxml.html.fragment_fromstring(rendered, create_parent='remove')

        for element in html.findall('.//*[@mod-meta]'):

            # Render nested template
            template_rendered, is_replace = self.render_element(element, data)

            original = lxml.html.tostring(element)
            if is_replace:
                replacement = template_rendered
            else:
                replacement = original
                replacement = replacement.replace('><', '>' + template_rendered + '<')

            rendered = rendered.replace(original, replacement)

        ## Parse HTML using html5lib; lxml is too strict and e.g. throws
        ## errors if missing parent container; htmlparser mangles whitespace
        ## and breaks replacement
        #parsed = BeautifulSoup(rendered, 'html5lib')
        #subtemplates = parsed.find_all(
        #    lambda tag: tag.has_attr('mod-meta')
        #)
        #
        #for element in subtemplates:
        #
        #    # Extract HTML of original element
        #    element_html = str(element)
        #
        #    # Render nested template
        #    template_rendered, is_replace = self.render_element(element, data)
        #
        #    # Build replacement
        #    if is_replace:
        #        replacement = template_rendered
        #    else:
        #        element.string = template_rendered
        #        replacement = str(element)
        #
        #    # Replace
        #    rendered = rendered.replace(element_html, replacement)

        return rendered

    def render(self, data, redirect_url, *args, **kwargs):
        """Render output of view function to HTML, following redirects
        and adding optional auxiliary data to view function response

        :param data: Data dictionary from view function
        :param redirect_url: Redirect URL; follow if not None
        :return: Rendered HTML
        """

        # Follow redirects
        if redirect_url is not None:
            return redirect(redirect_url)

        template_name = kwargs.get('template_name')

        # Load extra data
        extra_data = self.data if isinstance(self.data, dict) else self.data()
        data.update({key: val for key, val in extra_data.iteritems() if key not in data})

        return self._render(data, template_name)
