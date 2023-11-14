import certifi
import requests
from typing import Optional
from urllib.parse import urljoin
import urllib3

class API:
    base_url: str
    """Base API URL"""

    trailing_slash: bool=True
    """True if API URL requires a trailing slash"""

    datetime_string: bool=False
    """True if datetimes are serialized as strings"""

    session: requests.Session
    """Session object for API requests"""

    timeout: Optional[float] = 10.0
    """Default request timeout"""

    def __init__(self, url: str, proxy: Optional[str]=None):
        self.base_url = url

        self.reset_session()

        if proxy is not None:
            self.set_proxy(proxy)

    def set_proxy(self, proxy: str):
        proxies = { 'http': proxy
                  , 'https': proxy
                  }

        self.session.proxies.update(proxies)

    def reset_session(self, verify: bool=False):
        self.session = requests.Session()

        if verify:
            self.session.verify = certifi.where()
        else:
            self.session.verify = False
            urllib3.disable_warnings()

    def close(self):
        self.session.close()

    def _check_response(self, resp: requests.Response) -> requests.Response:
        resp.raise_for_status()
        return resp

    def get(self, uri: str, *args, **kwargs):
        kwargs.setdefault('timeout', self.timeout)
        req = self.session.get(urljoin(self.base_url, uri), *args, **kwargs)
        return self._check_response(req)

    def post(self, uri: str, *args, **kwargs):
        kwargs.setdefault('timeout', self.timeout)
        req = self.session.post(urljoin(self.base_url, uri), *args, **kwargs)
        return self._check_response(req)

    def patch(self, uri: str, *args, **kwargs):
        kwargs.setdefault('timeout', self.timeout)
        req= self.session.patch(urljoin(self.base_url, uri), *args, **kwargs)
        return self._check_response(req)

    def delete(self, uri: str, *args, **kwargs):
        kwargs.setdefault('timeout', self.timeout)
        req = self.session.delete(urljoin(self.base_url, uri), *args, **kwargs)
        return self._check_response(req)
