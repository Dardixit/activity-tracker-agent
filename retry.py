"""Retrying network errors: timeouts, dropped connections, DNS failures and
the APIs' "try again later" replies (HTTP 429 and 5xx).

Calls to Telegram, Google (Sheets, Calendar) and Claude go through call(), so
a flaky connection slows the bot down instead of stopping it. Each failure is
logged as one line; the wait between tries grows 2, 4, 8, ... up to 60 seconds.
"""

import socket
import ssl
import time

import anthropic
import google.auth.exceptions
import httplib2
import requests
from googleapiclient.errors import HttpError

MAX_DELAY = 60
# Within a run: 2+4+8+16+32+60+60 seconds of waiting, then the error goes to
# the run's own handler, which reports it on Telegram.
ATTEMPTS = 8
TRANSIENT_STATUS = (429, 500, 502, 503, 504)

NETWORK_ERRORS = (
    requests.RequestException,               # Telegram: timeouts, connection errors, DNS, non-JSON replies
    anthropic.APIConnectionError,            # Claude: connection errors and timeouts
    google.auth.exceptions.TransportError,   # Google token refresh could not reach Google
    httplib2.HttpLib2Error,                  # Google requests: ServerNotFoundError is a DNS failure
    TimeoutError, ConnectionError, socket.gaierror, ssl.SSLError,
)

# Replaced by tests so they do not wait.
sleep = time.sleep


def is_network_error(err):
    """True for errors that may go away if the same call is made again later."""
    if isinstance(err, HttpError):
        return err.resp.status in TRANSIENT_STATUS
    if isinstance(err, anthropic.APIStatusError):
        return err.status_code == 429 or err.status_code >= 500
    return isinstance(err, NETWORK_ERRORS)


def describe(err):
    """The error on one line."""
    text = str(err).strip().splitlines()
    return f"{type(err).__name__}: {text[0][:200]}" if text else type(err).__name__


def delay(failures):
    """Seconds to wait after this many failures in a row: 2, 4, 8, ... up to 60."""
    return min(2 ** failures, MAX_DELAY)


def call(what, fn, attempts=ATTEMPTS):
    """fn(), tried again after network errors. Other errors, and the network
    error of the last attempt, are raised."""
    for failure in range(1, attempts + 1):
        try:
            return fn()
        except Exception as err:
            if failure == attempts or not is_network_error(err):
                raise
            wait = delay(failure)
            print(f"{what} failed ({describe(err)}), trying again in {wait} s")
            sleep(wait)


def execute(request, what="Google API"):
    """A Google API request's execute(), tried again after network errors.
    Only for requests that are safe to repeat (reads); see Writer.commit."""
    return call(what, request.execute)
