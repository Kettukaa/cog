import datetime
import json
import logging
import multiprocessing
import os
import signal
import sys
import threading
import time
import traceback
from argparse import ArgumentParser
from typing import Any, Callable, Dict, Optional, Tuple

import requests
import structlog
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

from .. import schema
from ..server.webhook import webhook_caller
from .redis import EmptyRedisStream, RedisConsumer

log = structlog.get_logger(__name__)

# How often to check for model container setup on boot.
SETUP_POLL_INTERVAL = 0.1

# How often to check for cancelation or shutdown signals while a prediction is
# running, in seconds. 100ms mirrors the value currently supplied to the `poll`
# keyword argument for Worker.predict(...) in the redis queue worker code.
POLL_INTERVAL = 0.1


class QueueWorker:
    def __init__(
        self,
        redis_consumer: RedisConsumer,
        predict_timeout: int,
        prediction_event: threading.Event,
        shutdown_event: threading.Event,
        prediction_request_pipe: multiprocessing.connection.Connection,
    ):
        self.redis_consumer = redis_consumer

        self.prediction_event = prediction_event
        self.shutdown_event = shutdown_event
        self.prediction_request_pipe = prediction_request_pipe

        self.predict_timeout = predict_timeout

        self.cog_client = _make_local_http_client()
        self.cog_http_base = "http://localhost:5000"

    def start(self) -> None:
        mark = time.perf_counter()
        setup_poll_count = 0

        # First, we wait for the model container to report a successful
        # setup...
        while not self.shutdown_event.is_set():
            try:
                resp = requests.get(
                    self.cog_http_base + "/health-check",
                    timeout=1,
                )
            except requests.exceptions.RequestException:
                pass
            else:
                if resp.status_code == 200:
                    body = resp.json()

                    if (
                        body["status"] == "healthy"
                        and body["setup"] is not None
                        and body["setup"]["status"] == schema.Status.SUCCEEDED
                    ):
                        wait_seconds = time.perf_counter() - mark
                        log.info(
                            "model container completed setup", wait_seconds=wait_seconds
                        )

                        # FIXME: send setup-run webhook
                        break

            setup_poll_count += 1

            # Print a liveness message every five seconds
            if setup_poll_count % int(5 / SETUP_POLL_INTERVAL) == 0:
                wait_seconds = time.perf_counter() - mark
                log.info(
                    "waiting for model container to complete setup",
                    wait_seconds=wait_seconds,
                )

            time.sleep(SETUP_POLL_INTERVAL)

        # Now, we enter the main loop, pulling prediction requests from Redis
        # and managing the model container.
        while not self.shutdown_event.is_set():
            try:
                self.handle_message()
            except Exception:
                log.exception("failed to handle message")

        log.info("shutting down worker: bye bye!")

    def handle_message(self) -> None:
        try:
            message_id, message_json = self.redis_consumer.get()
        except EmptyRedisStream:
            time.sleep(POLL_INTERVAL)  # give the CPU a moment to breathe
            return

        message = json.loads(message_json)
        should_cancel = self.redis_consumer.checker(message.get("cancel_key"))
        prediction_id = message["id"]

        # Send the original request to the webserver, so it can trust the fields
        while self.prediction_request_pipe.poll():
            # clear the pipe first, out of an abundance of caution
            self.prediction_request_pipe.recv()
        self.prediction_request_pipe.send(message)

        # Reset the prediction event to indicate that a prediction is running
        self.prediction_event.clear()

        # Override webhook to call us
        message["webhook"] = "http://localhost:4900/webhook"

        # Call the untrusted container to start the prediction
        resp = self.cog_client.post(
            self.cog_http_base + "/predictions",
            json=message,
            headers={"Prefer": "respond-async"},
            timeout=2,
        )
        # FIXME: we should handle schema validation errors here and send
        # appropriate webhooks back up the stack.
        resp.raise_for_status()

        # Wait for any of: completion, shutdown signal. Also check to see if we
        # should cancel the running prediction, and make the appropriate HTTP
        # call if so.
        # FIXME: handle timeouts.
        while True:
            if self.prediction_event.wait(POLL_INTERVAL):
                break

            if should_cancel():
                resp = self.cog_client.post(
                    self.cog_http_base + "/predictions/" + prediction_id + "/cancel",
                    timeout=1,
                )
                resp.raise_for_status()

            if self.shutdown_event.is_set():
                return

        self.redis_consumer.ack(message_id)


def _make_local_http_client() -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(
        max_retries=Retry(
            total=3,
            backoff_factor=0.1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["POST"],
        ),
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session