import os
import logging

import click
import redis
from rq import Connection, Worker

LOGGER = logging.getLogger(__name__)

@click.command()
@click.option('--queue', '-q', 'queues', multiple=True)
def run_worker(queues):
    redis_connection = redis.Redis(
        os.getenv('REDIS', 'redis'),
        port=int(os.getenv('PORT', 6379)),
        db=0
    )
    LOGGER.debug("Running worker")
    with Connection(redis_connection):
        worker = Worker(queues)
        worker.work()

if __name__ == '__main__':
    run_worker()
