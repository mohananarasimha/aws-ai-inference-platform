import time

from config import (
    LAMBDA_RESERVE_SECONDS,
    MAX_QUEUED_SECONDS,
    MIN_CALL_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
)


def now():
    return time.monotonic()


class DeadlineExceeded(Exception):
    """The end-to-end request budget is used up. Carries no request data."""

    def __init__(self, component, step=None):
        super().__init__("request_deadline_exceeded")
        self.component = component
        self.step = step


class ComponentTimeout(Exception):
    """One downstream call hit its own timeout while budget may remain."""

    def __init__(self, component):
        super().__init__(f"{component}_timeout")
        self.component = component


class RequestDeadline:
    """
    One budget per request. Every downstream call asks it how long it may
    take: min(component maximum, time left).
    """

    def __init__(self, budget_seconds):
        self.budget = max(0.0, budget_seconds)
        self.started = now()
        self.step = None

    def elapsed(self):
        return now() - self.started

    def remaining(self):
        return self.budget - self.elapsed()

    def expired(self):
        return self.remaining() < MIN_CALL_SECONDS

    def check(self, component):
        if self.expired():
            raise DeadlineExceeded(component, self.step)

    def timeout(self, max_seconds, component):
        self.check(component)

        return min(max_seconds, self.remaining())


def call_timeout(deadline, max_seconds, component):
    """Deadline-aware timeout; without a deadline (tests, scripts) the maximum."""

    if deadline is None:
        return max_seconds

    return deadline.timeout(max_seconds, component)


def request_budget(context=None, queued_seconds=0.0):
    """
    The budget for one Lambda invocation: the configured request budget minus
    time already spent before the handler ran, and never more than what the
    Lambda itself has left minus the reserve.
    """

    budget = REQUEST_TIMEOUT_SECONDS - min(queued_seconds, MAX_QUEUED_SECONDS)

    remaining_ms = getattr(context, "get_remaining_time_in_millis", None)

    if callable(remaining_ms):
        budget = min(budget, remaining_ms() / 1000 - LAMBDA_RESERVE_SECONDS)

    return RequestDeadline(budget)
