import boto3
from botocore.config import Config

from config import AWS_CONNECT_TIMEOUT_SECONDS, REGION


_clients = {}


def aws_client(service, timeout):
    """
    boto3 client whose connect + read timeouts add up to at most `timeout`
    seconds (rounded down to a whole second, minimum 1). botocore cannot
    set a timeout per call, so one client is kept per service and size.

    Retries are off: a retry would get its own full timeout and break the
    request budget.
    """

    seconds = max(1, int(timeout))
    key = (service, seconds)

    if key not in _clients:
        connect = min(AWS_CONNECT_TIMEOUT_SECONDS, seconds / 3)

        _clients[key] = boto3.client(
            service,
            region_name=REGION,
            config=Config(
                connect_timeout=connect,
                read_timeout=seconds - connect,
                retries={"total_max_attempts": 1}
            )
        )

    return _clients[key]
