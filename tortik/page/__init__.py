# -*- coding: utf-8 -*-

import os
import sys
import urlparse
import traceback
from itertools import count
from functools import wraps, partial
from copy import copy
import json
import lxml.etree as etree
import tornado.web
import tornado.curl_httpclient
import tornado.httpclient
from tornado import stack_context
from tornado.options import options, define
from tortik import TORTIK_BASE_PATH
from tortik.util import decorate_all, make_list, real_ip, make_qs
from tortik.util.dumper import Dumper
from tortik.logger import PageLogger
from tortik.util.async import AsyncGroup
from tortik.util.parse import parse_xml, parse_json

stats = count()

define('debug_password', default=None, type=str, help='Password for debug')
define('debug', default=True, type=bool, help='Debug mode')
define('tortik_max_clients', default=200, type=int, help='Max clients (requests) for http_client')
define('tortik_timeout_multiplier', default=1.0, type=float, help='Timeout multiplier (affects all requests)')

_DEBUG_ALL = "all"
_DEBUG_ONLY_ERRORS = "only_errors"
_DEBUG_NONE = "none"


def preprocessors(method):
    @wraps(method)
    def wrapper(handler, *args, **kwargs):
        with stack_context.ExceptionStackContext(handler._handle_exception):
            def finished_cb():
                method(handler, *args, **kwargs)

            ag = AsyncGroup(finished_cb, log=handler.log.debug, name='preprocessors')
            for preprocessor in handler.preprocessors:
                preprocessor(handler, ag.add_empty_cb())

            ag.try_finish()

    return wrapper


class RequestHandler(tornado.web.RequestHandler):
    decorators = [
        (preprocessors, 'preprocessors'),
        (tornado.web.asynchronous, 'asynchronous'),  # should be the last
    ]
    __metaclass__ = decorate_all(decorators)

    def initialize(self, *args, **kwargs):
        debug_pass = options.debug_password
        debug_agrs = self.get_arguments('debug')
        debug_arg_set = (len(debug_agrs) > 0 and debug_pass is not None and
                         (debug_pass == '' or debug_pass == debug_agrs[-1]))

        if debug_arg_set:
            self.debug_type = _DEBUG_ALL
        elif options.debug:
            self.debug_type = _DEBUG_ONLY_ERRORS
        else:
            self.debug_type = _DEBUG_NONE

        if self.debug_type != _DEBUG_NONE and not hasattr(RequestHandler, 'debug_loader'):
            RequestHandler.debug_loader = self.create_template_loader(
                os.path.join(TORTIK_BASE_PATH, 'templates')
            )

        self.error_detected = False

        self.request_id = self.request.headers.get('X-Request-Id', str(stats.next()))

        self.log = PageLogger(self.request, self.request_id, (self.debug_type != _DEBUG_NONE),
                              handler_name=(type(self).__module__ + '.' + type(self).__name__))

        self.responses = {}
        self.http_client = self.initialize_http_client()

        self.preprocessors = copy(self.preprocessors) if hasattr(self, 'preprocessors') else []
        self.postprocessors = copy(self.postprocessors) if hasattr(self, 'postprocessors') else []

        self._extra_data = {}

    @staticmethod
    def get_global_http_client():
        if not hasattr(RequestHandler, '_http_client'):
            RequestHandler._http_client = tornado.curl_httpclient.CurlAsyncHTTPClient(
                max_clients=options.tortik_max_clients)
        return RequestHandler._http_client

    def initialize_http_client(self):
        return self.get_global_http_client()

    def add(self, name, data):
        self._extra_data[name] = data

    def get_data(self):
        return self._extra_data

    def compute_etag(self):
        return None

    def on_finish(self):  # tornado 2.2+
        self.log.complete_logging(self.get_status())

    def _handle_exception(self, type, value, tb):
        if self.error_detected is True:  # prevent error infinite loop
            self.log.error("Exception already detected")
            if not self._finished:
                self.finish()
            return

        self.error_detected = True
        if self.debug_type in [_DEBUG_ALL, _DEBUG_ONLY_ERRORS]:
            self.log.error("Uncaught exception %s\n%r", self._request_summary(),
                           self.request, exc_info=(type, value, tb))

            if self._finished:
                return

            response_code = value.status_code if isinstance(value, tornado.web.HTTPError) else 500
            self.set_status(response_code)
            self.log.complete_logging(response_code)
            self.finish_with_debug()

            return True

    def finish_with_debug(self):
        self.set_header('Content-Type', 'text/html; charset=utf-8')
        if self.debug_type == _DEBUG_ALL:
            self.set_status(200)

        self.finish(RequestHandler.debug_loader.load('debug.html').generate(
            data=self.log.get_debug_info(),
            output_data=self.get_data(),
            size=sys.getsizeof,
            get_params=lambda x: urlparse.parse_qs(x, keep_blank_values=True),
            pretty_json=lambda x: json.dumps(x, sort_keys=True, indent=4, ensure_ascii=False),
            pretty_xml=lambda x: etree.tostring(x, pretty_print=True, encoding=unicode),
            dumper=Dumper.dump,
            format_exception=lambda x: "".join(traceback.format_exception(*x))
        ))

    def complete(self, output_data=None):
        with stack_context.ExceptionStackContext(self._handle_exception):
            def finished_cb(handler, data):
                handler.log.complete_logging(handler.get_status())
                if handler.debug_type == _DEBUG_ALL:
                    self.finish_with_debug()
                    return

                self.finish(data)

            if self.postprocessors:
                last = len(self.postprocessors) - 1

                def add_cb(index):
                    if index == last:
                        return finished_cb
                    else:
                        def _cb(handler, data):
                            self.postprocessors[index + 1](handler, data, add_cb(index + 1))
                        return _cb

                self.postprocessors[0](self, output_data, add_cb(0))
            else:
                finished_cb(self, output_data)

    def fetch_requests(self, requests, callback=None, stage='page'):
        self.log.stage_started(stage)
        requests = make_list(requests)

        def _finish_cb():
            self.log.stage_complete(stage)
            if callback is not None:
                callback()

        ag = AsyncGroup(_finish_cb, self.log.debug, name=stage)

        def _on_fetch(response, name):
            content_type = response.headers.get('Content-Type', '').split(';')[0]
            response.data = None
            try:
                if 'xml' in content_type:
                    response.data = parse_xml(response)
                elif content_type == 'application/json':
                    response.data = parse_json(response)
            except:
                self.log.warning('Could not parse response with Content-Type header')

            if response.data is not None:
                self.add(name, response.data)

            self.responses[name] = response
            self.log.request_complete(response)

        for req in requests:
            if isinstance(req, (tuple, list)):
                assert len(req) in (2, 3)
                req = self.make_request(name=req[0], method='GET', full_url=req[1],
                                        data=req[2] if len(req) == 3 else '')
            self.log.request_started(req)
            self.http_client.fetch(req, ag.add(partial(_on_fetch, name=req.name)))

    def make_request(self, name, method='GET', full_url=None, url_prefix=None, path='', data='', headers=None,
                     connect_timeout=0.5, request_timeout=2, follow_redirects=True):

        if (full_url is None) == (url_prefix is None):
            raise TypeError('make_request required path/url_prefix arguments pair or full_url argument')
        if full_url is not None and path != '':
            raise TypeError("Can't combine full_url and path arguments")

        scheme = 'http'
        query = ''
        body = None

        if full_url is not None:
            parsed_full_url = urlparse.urlsplit(full_url)
            scheme = parsed_full_url.scheme
            url_prefix = parsed_full_url.netloc
            path = parsed_full_url.path
            query = parsed_full_url.query

        if method in ['GET', 'HEAD']:
            parsed_query = urlparse.parse_qs(query)
            parsed_query.update(data if isinstance(data, dict) else urlparse.parse_qs(data))
            query = make_qs(parsed_query)
        else:
            body = make_qs(data) if isinstance(data, dict) else data

        headers = {} if headers is None else headers

        headers.update({
            'X-Forwarded-For': real_ip(self.request),
            'X-Request-Id': self.request_id,
            'Content-Type': headers.get('Content-Type', 'application/x-www-form-urlencoded')
        })

        req = tornado.httpclient.HTTPRequest(
            url=urlparse.urlunsplit((scheme, url_prefix, path, query, '')),
            method=method,
            headers=headers,
            body=body,
            connect_timeout=connect_timeout*options.tortik_timeout_multiplier,
            request_timeout=request_timeout*options.tortik_timeout_multiplier,
            follow_redirects=follow_redirects
        )
        req.name = name
        return req

    def add_preprocessor(self, preprocessor):
        self.preprocessors.append(preprocessor)

    def add_postprocessor(self, postprocessor):
        self.postprocessors.append(postprocessor)
