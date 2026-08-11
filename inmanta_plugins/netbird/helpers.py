"""
Copyright 2026 Guillaume Everarts de Velp

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Contact: edvgui@gmail.com
"""

import json
import logging
import typing
import urllib.parse

import pydantic
import requests
import requests.adapters
import requests.auth
import urllib3.util.retry

from inmanta.agent.handler import LoggerABC, PythonLogger

LOGGER = logging.getLogger(__name__)


class RequestValues(typing.TypedDict):
    request_method: str
    request_url: str
    request_headers: dict[str, str]
    request_body: str | None


class ResponseValues(typing.TypedDict):
    response_status: int
    response_reason: str
    response_headers: dict[str, str]
    response_body: str | None


def format_body(body: object) -> str | None:
    """
    Try to format the body of a request or response into something that
    can be useful to a human reading logs.  Anything that is not text (no body at
    all, a binary body, a stream) is dropped.
    """
    if not isinstance(body, str):
        return None

    try:
        # If the body is json, format it
        return json.dumps(json.loads(body), indent=4)
    except json.JSONDecodeError:
        pass

    return body


class Session(requests.Session):
    """
    A requests session that knows the base url of the api it talks to, logs every
    exchange in the handler logger with the secrets redacted, and can serve the
    GET requests of one deploy from a cache.
    """

    def __init__(
        self,
        base_url: str | None = None,
        logger: LoggerABC | None = None,
        timeout: int | None = None,
        secrets: typing.Mapping[str, str] | None = None,
        cache: bool = False,
    ) -> None:
        super().__init__()
        self.base_url = base_url
        self.logger = logger or PythonLogger(LOGGER)
        self.timeout = timeout
        self.secrets = secrets or dict()
        self.cache = cache
        self.responses: dict[str, requests.Response] = dict()

        # Retry transient network failures (connection resets, dropped keep-alive
        # sockets, gateway errors) on every method.  The writes this module sends are
        # full desired states, so they are safe to retry.
        retry = urllib3.util.retry.Retry(
            total=5,
            connect=5,
            read=3,
            status=3,
            backoff_factor=1.0,
            status_forcelist=(502, 503, 504),
            allowed_methods=frozenset(
                {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
            ),
            raise_on_status=False,
        )
        adapter = requests.adapters.HTTPAdapter(max_retries=retry)
        self.mount("https://", adapter)
        self.mount("http://", adapter)

    def prepare_request(self, request: requests.Request) -> requests.PreparedRequest:
        """
        Extend the behavior of the prepare_request method to insert the base url
        when there is one defined.  Delegate the rest of the request preparation
        to the parent implementation.
        """
        if self.base_url is not None and isinstance(request.url, str):
            # If we have a base url, use it to extend the current url
            request.url = urllib.parse.urljoin(self.base_url, request.url)

        return super().prepare_request(request)

    def redacted(self, value: str) -> str:
        """
        Replace any instance of any of the known secrets by its redacted equivalent.
        """
        for secret, redacted in self.secrets.items():
            value = value.replace(secret, redacted)

        return value

    def request_values(self, request: requests.PreparedRequest) -> RequestValues:
        """
        Extract the values that should be logged from the request, make sure that
        any data containing secret is redacted.
        """
        # Format and redact the body
        body = format_body(request.body)
        if body is not None:
            body = self.redacted(body)

        # Redact the headers
        headers = {
            k: self.redacted(v if isinstance(v, str) else v.decode())
            for k, v in request.headers.items()
        }

        return {
            "request_url": self.redacted(
                pydantic.TypeAdapter(str).validate_python(request.url),
            ),
            "request_method": self.redacted(
                pydantic.TypeAdapter(str).validate_python(request.method),
            ),
            "request_headers": headers,
            "request_body": body,
        }

    def response_values(self, response: requests.Response) -> ResponseValues:
        """
        Extract the values that should be logged from the response, make sure that
        any data containing secret is redacted.
        """
        # Format and redact the body
        body = format_body(response.text) if response.encoding else None
        if body is not None:
            body = self.redacted(body)

        # Redact the headers
        headers = {k: self.redacted(v) for k, v in response.headers.items()}

        return {
            "response_status": response.status_code,
            "response_reason": response.reason,
            "response_headers": headers,
            "response_body": body,
        }

    def _send(
        self,
        request: requests.PreparedRequest,
        **kwargs: object,
    ) -> requests.Response:
        """
        Actual send implementation, moved into a helper method to implement
        the caching more easily.
        """
        if "timeout" not in kwargs and self.timeout is not None:
            kwargs["timeout"] = self.timeout

        # Process request logged values
        logged_request_values = self.request_values(request)

        try:
            response = super().send(request, **kwargs)
        except requests.RequestException as e:
            self.logger.debug(
                "%(request_method)s %(request_url)s: %(exception)s",
                exc_info=False,
                exception=str(e),
                **logged_request_values,
            )
            raise e

        # Log the request
        self.logger.debug(
            "%(request_method)s %(request_url)s: %(response_status)d (%(response_reason)s)",
            **logged_request_values,
            **self.response_values(response),
        )

        return response

    def send(
        self,
        request: requests.PreparedRequest,
        **kwargs: object,
    ) -> requests.Response:
        """
        Extend the behavior of the send method to insert a default timeout when none
        is defined, and to log the request and response in the attached logger, if any.
        """
        url = request.url
        if not self.cache or request.method != "GET" or url is None:
            # Anytime we push data, we reset the cache, sub-optimal but already
            # better than no caching, and where the desired state doesn't change it
            # saves lots of calls to the api
            self.responses.clear()
            return self._send(request, **kwargs)

        if url in self.responses:
            # If we have a hit for the url
            return self.responses[url]

        # Nothing in the cache, send the request
        response = self._send(request, **kwargs)
        self.responses[url] = response

        return response
