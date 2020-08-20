# =================================================================
#
# Authors: Richard Law <lawr@landcareresearch.co.nz>
#
# Copyright (c) 2020 Richard Law
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# =================================================================

from datetime import datetime
import json
import logging

import redis
from rq import Queue
from rq.job import Job

from pygeoapi.util import JobStatus
from pygeoapi.process.manager.base import (
    BaseManager, ManagerExecuteError, DATETIME_FORMAT
)

LOGGER = logging.getLogger(__name__)

def make_key(processid='*', job_id='*'):
    """
    Concatenates a process ID and a job ID into a static key for use in Redis

    :param processid: process identifier, or '*' as wildcard
    :param jobid: job identifier, or '*' as wildcard

    :returns: `str` concatenation like `'process:{processid}:job:{job_id}'`
    """
    return f'process:{processid or "*"}:job:{job_id or "*"}'

def dict_remove_none(_dict):
    """
    Removes key-value pairs from a dictionary where the value is `None`. Does
    not handle nested dictionaries.

    :param _dict: `dict`

    :returns: `dict`
    """
    return {k: v for k, v in _dict.items() if v is not None}

def _job_state_from_queue(job):
    """
    Converts an RQ Job into a dict representing the appropriate server state

    :param job: an RQ Job (https://python-rq.org/docs/jobs/)

    :return dict: server state, reflecting pygeoapi API for processing jobs
    """
    job_status = job.get_status()
    if job_status == 'queued':
        state = {
            'status': JobStatus.accepted.value,
            'message': f'Job queued at {job.enqueued_at.strftime(DATETIME_FORMAT)}',
            'progress': 10
        }
    elif job_status == 'deferred':
        state = {
            'status': JobStatus.accepted.value,
            'message': 'Job is deferred',
            'progress': 12
        }
    elif job_status == 'started':
        state = {
            'status': JobStatus.running.value,
            'message': f'Job started at {job.started_at.strftime(DATETIME_FORMAT)}',
            'progress': 15
        }
    elif job_status == 'finished':
        state = {
            'status': JobStatus.successful.value,
            'message': 'Job is finished',
            'progress': 100,
            'process_end_datetime': job.ended_at.strftime(DATETIME_FORMAT)
        }
    else:
        state = {
            'status': JobStatus.failed.value,
            'message': f'Job failed: {job.exc_info}',
            'progress': 100,
            'process_end_datetime': None if not job.ended_at else job.ended_at.strftime(DATETIME_FORMAT)
        }
    return state

class RedisManager(BaseManager):
    """Redis Manager"""

    def __init__(self, manager_def):
        """
        Initialize object

        :param manager_def: manager definition

        :returns: `pygeoapi.process.manager.base.BaseManager`
        """

        BaseManager.__init__(self, manager_def)

        self.connection = manager_def['connection']
        self.connect()

    def connect(self):
        """
        connect to manager

        :returns: `bool` of status of result
        """

        self.db = redis.ConnectionPool(
            host=self.connection,
            port=6379,
            db=0,
            decode_responses=True,
        )
        return True

    def _connect(self, pool=True, decode_responses=True):
        try:
            if pool:
                LOGGER.debug(f'Connecting to Redis connection pool: {self.db}')
                return redis.Redis(connection_pool=self.db, socket_connect_timeout=10)
            else:
                LOGGER.debug(f'Connecting to Redis: {self.connection}')
                return redis.Redis(self.connection, socket_connect_timeout=10, port=6379, db=0, decode_responses=decode_responses)
        except redis.exceptions.ConnectionError as exc:
            # TODO - wait, retry, return 503 (Service Unavailable)?
            LOGGER.debug(exc)
            raise ManagerExecuteError('Redis connection error')

    def _connect_queue(self, processid):
        """
        :param processid: process identifier, used as name of the queue

        :returns RQ Queue: https://python-rq.org/docs/
        """
        db = self._connect(pool=False, decode_responses=False)
        return Queue(processid, connection=db)

    def destroy(self):
        """
        Destroy manager

        :returns: `bool` status of result
        """
        # This manager uses a Redis connection pool that does not need to be
        # explicitly closed
        return True

    def get_jobs(self, processid=None, status=None):
        """
        Get jobs

        :param processid: process identifier
        :param status: job status (accepted, running, successful,
                       failed, results) (default is all)

        :returns: list of jobs
        """
        self._updates_from_job_queue(processid or '*')
        db = self._connect()
        jobs = []
        match = make_key(processid or '*', '*')
        for key in db.scan_iter(match):
            data = db.hgetall(key)
            if status and data.get('status') != status:
                continue
            jobs.append(data)
        return jobs

    def add_job(self, job_metadata):
        """
        Add a job

        :param job_metadata: `dict` of job metadata

        :returns: `bool` of add job result
        """
        db = self._connect()
        job_id = job_metadata.get('identifier')
        processid = job_metadata.get('processid')
        key = make_key(processid, job_id)
        hmset_status = db.hmset(key, dict_remove_none(job_metadata))
        return hmset_status

    def update_job(self, processid, job_id, update_dict):
        """
        Updates a job

        :param processid: process identifier
        :param job_id: job identifier
        :param update_dict: `dict` of property updates

        :returns: `bool` of status result
        """
        db = self._connect()
        key = make_key(processid, job_id)
        update_dict = dict_remove_none(update_dict)
        # dicts are serialised before being added to redis, so the update_dict
        # cannot be used to partially update; first HGETALL the record and then
        # create a new, complete, update dict
        with db.pipeline() as pipe:
            # Context manager for pipe calls reset() automatically to return
            # the connection to the pool
            while True:
                try:
                    pipe.watch(key)
                    current_data = pipe.hgetall(key)
                    update_data = {**current_data, **update_dict}
                    pipe.multi()
                    pipe.hmset(key, update_data)
                    hmset_status = pipe.execute()
                    break
                except redis.exceptions.WatchError:
                    # The key was updated between being read and updated with
                    # the new information
                    # TODO could retry
                    # TODO better error message
                    raise ManagerExecuteError('Update atomicity error')
        return hmset_status

    def _execute_handler_async(self, p, job_id, data_dict):
        """
        Asynchronous process execution handler that puts job data onto a queue.
        Workers (running elsewhere) may take jobs off this queue and perform
        the actual process execution logic.

        This function will initially and synchonroously update the persistent
        Redis store about the acceptance of the job, then put the job on the
        queue, and then immediately return without waiting for the job to
        complete. Future updates to the persistent Redis store (with actual
        job outputs) will be performed (synchonrously) with the method
        RedisManager._updates_from_job_queue

        :param p: `pygeoapi.process` object
        :param job_id: job identifier
        :param data_dict: `dict` of data parameters

        :returns: tuple of (initial response payload, typically None)
                  and JobStatus (i.e. initial job status, typically
                  JobStatus.accepted)
        """
        LOGGER.debug('_execute_handler_async')
        processid = p.metadata['id']
        current_status = JobStatus.accepted
        job_metadata = {
            'identifier': job_id,
            'processid': processid,
            'process_start_datetime': datetime.utcnow().strftime(DATETIME_FORMAT),
            'process_end_datetime': None,
            'status': current_status.value,
            'location': None,
            'message': 'Job about to be placed on queue',
            'progress': 1
        }
        self.add_job(job_metadata)
        try:
            q = self._connect_queue(processid)
            job = q.enqueue(
                p.execute,
                job_id=job_id,
                timeout='5m',
                args=(data_dict,),
                kwargs={}
            )
            LOGGER.debug(f'there are {len(q)} jobs on the queue')
            job_state = _job_state_from_queue(job)
            self.update_job(processid, job_id, job_state)
            outputs = None # job.result
        except Exception as err:
            # TODO assess correct exception type and description to help users
            # NOTE, the /results endpoint should return the error HTTP status
            # for jobs that failed, ths specification says that failing jobs
            # must still be able to be retrieved with their error message
            # intact, and the correct HTTP error status at the /results
            # endpoint, even if the /result endpoint correctly returns the
            # failure information (i.e. what one might assume is a 200
            # response).
            LOGGER.exception(err)
            current_status = JobStatus.failed
            code = 'InvalidParameterValue'
            outputs = {
                'code': code,
                'description': str(err) # NOTE this is optional and internal exceptions aren't useful for (or safe to show) end-users
            }
            LOGGER.error(outputs)
            job_metadata = {
                'process_end_datetime': datetime.utcnow().strftime(DATETIME_FORMAT),
                'status': current_status.value,
                'location': None,
                'message': f'{code}: {outputs["description"]}'
            }

            self.update_job(processid, job_id, job_metadata)
        return outputs, current_status

    def _execute_handler(self, p, job_id, data_dict):
        """
        Process execution handler that performs execution synchronously, and
        updates the persistent redis store, before and after the processing is
        actually performed.
        """
        LOGGER.debug('_execute_handler')
        processid = p.metadata['id']
        current_status = JobStatus.accepted
        job_metadata = {
            'identifier': job_id,
            'processid': processid,
            'process_start_datetime': datetime.utcnow().strftime(DATETIME_FORMAT),
            'process_end_datetime': None,
            'status': current_status.value,
            'location': None,
            'message': 'Job accepted and ready for execution',
            'progress': 5
        }
        self.add_job(job_metadata)

        try:
            current_status = JobStatus.running
            outputs = list(map(dict_remove_none, p.execute(data_dict)))
            self.update_job(processid, job_id, {
                'status': current_status.value,
                'message': 'Writing job output',
                'progress': 95
            })

            # Write output to redis as serialised JSON
            db = self._connect()
            output_key = f'output:{make_key(processid, job_id)}'
            db.set(output_key, json.dumps(outputs, sort_keys=True, indent=4))

            current_status = JobStatus.finished
            job_update_metadata = {
                'process_end_datetime': datetime.utcnow().strftime(DATETIME_FORMAT),
                'status': current_status.value,
                'location': output_key,
                'message': 'Job complete',
                'progress': 100
            }

            self.update_job(processid, job_id, job_update_metadata)

        except Exception as err:
            # TODO assess correct exception type and description to help users
            # NOTE, the /results endpoint should return the error HTTP status
            # for jobs that failed, ths specification says that failing jobs
            # must still be able to be retrieved with their error message
            # intact, and the correct HTTP error status at the /results
            # endpoint, even if the /result endpoint correctly returns the
            # failure information (i.e. what one might assume is a 200
            # response).
            LOGGER.exception(err)
            current_status = JobStatus.failed
            code = 'InvalidParameterValue'
            outputs = {
                'code': code,
                'description': str(err) # NOTE this is optional and internal exceptions aren't useful for (or safe to show) end-users
            }
            LOGGER.error(outputs)
            job_metadata = {
                'process_end_datetime': datetime.utcnow().strftime(DATETIME_FORMAT),
                'status': current_status.value,
                'location': None,
                'message': f'{code}: {outputs["description"]}'
            }

            self.update_job(processid, job_id, job_metadata)

        return outputs, current_status

    def execute_process(self, p, job_id, data_dict, sync=True):
        """
        Process execution handler

        :param p: `pygeoapi.process` object
        :param job_id: job identifier
        :param data_dict: `dict` of data parameters
        :param sync: `bool` specifying sync or async processing.

        :returns: tuple of response payload and status
        """
        return super(RedisManager, self).execute_process(p, job_id, data_dict, sync=sync)

    def delete_job(self, processid, job_id):
        """
        Deletes a job

        :param processid: process identifier
        :param job_id: job identifier

        :return `bool` of status result
        """
        db = self._connect()
        key = make_key(processid, job_id)
        output_key = f'output:{key}'
        del_status = db.delete(key, output_key)
        return del_status

    def delete_jobs(self, max_jobs, older_than):
        """
        TODO
        """
        raise NotImplementedError()

    def _update_job_from_registry(self, processid, registry, job, job_id, persist_job=False, delete_job=False):
        """
        Responsible for persisting job output, persisting job status, and
        also removing jobs from queue registries.

        :param processid: process identifier
        :param registry: RQ JobRegistry (https://python-rq.org/docs/job_registries/)
        :param job: RQ Job (https://python-rq.org/docs/)
        :param job_id: string job identifier, usually a UUID
        :param persist_job: bool, if True, will check job.result for output and
                            write this to peristent Redis cache for indefinite
                            retrieval. Defualts to False.
        :param delete_job: bool, if True, will remove the job from the
                           `registry`, so the workers will no longer have any
                           record of the job's existence. Useful as cleanup when
                           the `registry` represents finished or failed jobs.
                           Defaults to False.

        :returns None: This function only has side-effects, manipulating state.
        """
        if persist_job:
            db = self._connect(pool=False, decode_responses=False)
            output_key = f'output:{make_key(processid, job_id)}'
            db.set(output_key, json.dumps(job.result, sort_keys=True, indent=4))

        job_state = _job_state_from_queue(job)
        self.update_job(processid, job_id, job_state)
        if delete_job:
            registry.remove(job_id, delete_job=True)
        return

    def _updates_from_job_queue(self, processid):
        """
        Checks the job queues, and updates redis state.

        This could be replaced by some other kind of watcher that responds to
        worker signals; this is not an ideal implementation since it depends on
        clients requesting jobs and then checking the queues.

        :param processid: process identifier

        :returns None: This function only has side-effects, manipulating state
        """
        db = self._connect(pool=False, decode_responses=False)
        q = self._connect_queue(processid)

        registries = [
            q.finished_job_registry,
            q.failed_job_registry,
            q.started_job_registry,
            q.deferred_job_registry,
            q.scheduled_job_registry
        ]

        for registry in registries:
            job_ids = registry.get_job_ids()
            jobs = Job.fetch_many(job_ids, connection=db)
            for job_id, job in zip(job_ids, jobs):
                persist_job = registry == q.finished_job_registry or registry == q.failed_job_registry
                delete_job = persist_job
                self._update_job_from_registry(
                    processid, registry, job, job_id,
                    persist_job=persist_job,
                    delete_job=delete_job
                )
        return

    def get_job_result(self, processid, job_id):
        """
        Get a single job

        :param processid: process identifier
        :param jobid: job identifier

        :returns: `dict`
        """
        self._updates_from_job_queue(processid)
        db = self._connect()
        key = make_key(processid, job_id)
        return db.hgetall(key)

    def get_job_output(self, processid, job_id):
        self._updates_from_job_queue(processid)
        job_result = self.get_job_result(processid, job_id)
        if not job_result:
            # processs/job does not exist
            return None, None
        job_status = JobStatus[job_result['status']]
        if not job_status == JobStatus.successful:
            # Job is incomplete
            return job_status, None
        db = self._connect()
        key = job_result.get('location', f'output:{make_key(processid, job_id)}')
        result = json.loads(db.get(key))
        return job_status, result


    def __repr__(self):
        return '<RedisManager> {}'.format(self.name)
