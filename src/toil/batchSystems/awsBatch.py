# Copyright (C) 2015-2021 Regents of the University of California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Batch system for running Toil workflows on AWS Batch.

Useful with the AWS job store.

AWS Batch has no means for scheduling based on disk usage, so the backing
machines need to have "enough" disk and other constraints need to guarantee
that disk does not fill.

Assumes that an AWS Batch Queue name or ARN is already provided.

Handles creating and destroying a JobDefinition for the workflow run.

Additional containers should be launched with Singularity, not Docker.
"""
import base64
import datetime
import logging
import math
import os
import pickle
import tempfile
import time
import uuid
from argparse import ArgumentParser, _ArgumentGroup
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Union

from requests.exceptions import HTTPError
from boto.exception import BotoServerError

from toil import applianceSelf
from toil.batchSystems.abstractBatchSystem import (EXIT_STATUS_UNAVAILABLE_VALUE,
                                                   BatchJobExitReason,
                                                   UpdatedBatchJobInfo)
from toil.batchSystems.cleanup_support import BatchSystemCleanupSupport
from toil.common import Config, Toil
from toil.job import JobDescription
from toil.lib.aws import get_current_aws_zone, zone_to_region
from toil.lib.aws.session import establish_boto3_session
from toil.lib.conversions import b_to_mib, mib_to_b
from toil.lib.misc import slow_down, utc_now, unix_now_ms
from toil.lib.retry import retry
from toil.resource import Resource

logger = logging.getLogger(__name__)


# Map from AWS Batch terminal states to Toil batch job exit reasons
STATE_TO_EXIT_REASON: Dict[str, BatchJobExitReason] = {
    'SUCCEEDED': BatchJobExitReason.FINISHED,
    'FAILED': BatchJobExitReason.FAILED
}

# What's the max polling list size?
MAX_POLL_COUNT = 100

# AWS batch won't accept API requests asking for less than this much memory.
MIN_REQUESTABLE_MIB = 4
# AWS batch won't accept API requests asking for less than this many CPUs.
MIN_REQUESTABLE_CORES = 1

class AWSBatchBatchSystem(BatchSystemCleanupSupport):
    @classmethod
    def supportsAutoDeployment(cls) -> bool:
        return True

    def __init__(self, config: Config, maxCores: float, maxMemory: int, maxDisk: int) -> None:
        super().__init__(config, maxCores, maxMemory, maxDisk)

        # Determine region to use.
        # Either it's set specifically or maybe we can get it from the "best" zone.
        # TODO: Parse it from a full queue ARN?
        self.region = getattr(config, 'aws_batch_region')
        if self.region is None:
            zone = get_current_aws_zone()
            if zone is None:
                # Can't proceed without a real zone
                raise RuntimeError('To use AWS Batch, specify --awsBatchRegion or '
                                   'TOIL_AWS_BATCH_REGION or TOIL_AWS_ZONE, or configure '
                                   'a default zone in boto')
            self.region = zone_to_region(zone)

        # Connect to AWS Batch.
        # TODO: Use a global AWSConnectionManager so we can share a client
        # cache with provisioners, etc.
        self.client = establish_boto3_session(self.region).client('batch')

        # Determine our batch queue
        self.queue = getattr(config, 'aws_batch_queue')
        if self.queue is None:
            # Make sure we actually have a queue
            raise RuntimeError("To use AWS Batch, --awsBatchQueue or TOIL_AWS_BATCH_QUEUE must be set")
        # And the role, if any, jobs should assume
        self.job_role_arn = getattr(config, 'aws_batch_job_role_arn')
        # And the Owner tag value, if any, to apply to things we create
        self.owner_tag = os.environ.get('TOIL_OWNER_TAG')

        # Try and guess what Toil work dir the workers will use.
        # We need to be able to provision (possibly shared) space there.
        # TODO: Deduplicate with Kubernetes batch system.
        self.worker_work_dir = Toil.getToilWorkDir(config.workDir)
        if (config.workDir is None and
            os.getenv('TOIL_WORKDIR') is None and
            self.worker_work_dir == tempfile.gettempdir()):

            # We defaulted to the system temp directory. But we think the
            # worker Dockerfiles will make them use /var/lib/toil instead.
            # TODO: Keep this in sync with the Dockerfile.
            self.worker_work_dir = '/var/lib/toil'

        # We assign job names based on a numerical job ID. This functionality
        # is managed by the BatchSystemLocalSupport.

        # Here is where we will store the user script resource object if we get one.
        self.user_script: Optional[Resource] = None

        # Get the image to deploy from Toil's configuration
        self.docker_image = applianceSelf()

        # We can't use AWS Batch without a job definition. But we can use one
        # of them for all the jobs. We want to lazily initialize it. This will
        # be an ARN.
        self.job_definition: Optional[str] = None

        # We need a way to map between our batch system ID numbers, and AWS Batch job IDs from the server.
        self.bs_id_to_aws_id: Dict[int, str] = {}
        self.aws_id_to_bs_id: Dict[str, int] = {}
        # We need to track if jobs were killed so they don't come out as updated
        self.killed_job_aws_ids: Set[str] = set()

    def setUserScript(self, user_script: Resource) -> None:
        logger.debug('Setting user script for deployment: {}'.format(user_script))
        self.user_script = user_script

    # setEnv is provided by BatchSystemSupport, updates self.environment

    def issueBatchJob(self, job_desc: JobDescription, job_environment: Optional[Dict[str, str]] = None) -> int:
        # Try the job as local
        local_id = self.handleLocalJob(job_desc)
        if local_id is not None:
            # It is a local job
            return local_id
        else:
            # We actually want to send to the cluster

            # Check resource requirements (managed by BatchSystemSupport)
            self.checkResourceRequest(job_desc.memory, job_desc.cores, job_desc.disk)

            # Make a batch system scope job ID
            bs_id = self.getNextJobID()
            # Make a vaguely human-readable name.
            # We could add a per-workflow prefix to use with ListTasks, but
            # ListTasks doesn't let us filter for newly done tasks, so it's not
            # actually useful for us over polling each task.
            job_name = self._ensafen_name(str(job_desc))

            # Launch the job on AWS Batch

            # Determine job environment
            environment = self.environment.copy()
            if job_environment:
                environment.update(job_environment)

            # Make a job dict to send to the executor.
            # TODO: Factor out executor setup from here and Kubernetes and TES
            job: Dict[str, Any] = {"command": job_desc.command}
            if self.user_script is not None:
                # If there's a user script resource be sure to send it along
                job['userScript'] = self.user_script
            # Encode it in a form we can send in a command-line argument. Pickle in
            # the highest protocol to prevent mixed-Python-version workflows from
            # trying to work. Make sure it is text so we can ship it to Kubernetes
            # via JSON.
            encoded_job = base64.b64encode(pickle.dumps(job, pickle.HIGHEST_PROTOCOL)).decode('utf-8')
            # Make a command to run it in the exacutor
            command_list = ['_toil_contained_executor', encoded_job]

            # Compose a job spec to submit
            job_spec = {
                'jobName': job_name,
                'jobQueue': self.queue,
                'jobDefinition': self._get_or_create_job_definition(),
                'containerOverrides': {
                    'command': command_list,
                    'environment': [{'name': k, 'value': v} for k, v in environment.items()],
                    'resourceRequirements': [
                        {'type': 'MEMORY', 'value': str(max(MIN_REQUESTABLE_MIB, math.ceil(b_to_mib(job_desc.memory))))},
                        {'type': 'VCPU', 'value': str(max(MIN_REQUESTABLE_CORES, math.ceil(job_desc.cores)))}
                    ]
                }
            }
            if self.owner_tag:
                # We are meant to tag everything with an owner
                job_spec['tags'] = {'Owner': self.owner_tag}

            # Launch it and get back the AWS ID that we can use to poll the task.
            # TODO: retry!
            response = self.client.submit_job(**job_spec)
            aws_id = response['jobId']

            # Tie it to the numeric ID
            self.bs_id_to_aws_id[bs_id] = aws_id
            self.aws_id_to_bs_id[aws_id] = bs_id

            logger.debug('Launched job: %s', job_name)

            return bs_id

    @staticmethod
    def _ensafen_name(input_name: str) -> str:
        """
        Internal function. Should not be called outside this class.

        Make a job name safe for Amazon Batch.
        From the API docs:

            It can be up to 128 letters long. The first character must be
            alphanumeric, can contain uppercase and lowercase letters, numbers,
            hyphens (-), and underscores (_).
        """
        # Do replacements to enhance readability
        input_name = input_name.replace(" ", "-")
        # Keep only acceptable characters
        kept_chars = [c for c in input_name if c.isalnum() or c == '-' or c == '_']
        if len(kept_chars) == 0 or not kept_chars[0].isalnum():
            # Make sure we start with something alphanumeric
            kept_chars = ['j'] + kept_chars
        # Keep no more than the limit of them
        kept_chars = kept_chars[:128]
        # And re-compose them into a string
        return ''.join(kept_chars)

    def _get_runtime(self, job_detail: Dict[str, Any]) -> Optional[float]:
        """
        Internal function. Should not be called outside this class.

        Get the time that the given job ran/has been running for, in seconds,
        or None if that time is not available. Never returns 0.

        Takes an AWS JobDetail as a dict.
        """

        if 'status' not in job_detail or job_detail['status'] not in ['STARTING', 'RUNNING', 'SUCCEEDED', 'FAILED']:
            # Job is not running yet.
            logger.info("Runtime unavailable because job is still waiting")
            return None

        if 'startedAt' not in job_detail:
            # Job has no known start time
            logger.info("Runtime unavailable because job has no start time")
            return None

        start_ms = job_detail['startedAt']

        if 'stoppedAt' in job_detail:
            end_ms = job_detail['stoppedAt']
        else:
            end_ms = unix_now_ms()

        # We have a set start time, so it is/was running.
        runtime = slow_down((end_ms - start_ms) / 1000)
        # Return the time it has been running for.
        return runtime

    def _get_exit_code(self, job_detail: Dict[str, Any]) -> int:
        """
        Internal function. Should not be called outside this class.

        Get the exit code of the given JobDetail, or
        EXIT_STATUS_UNAVAILABLE_VALUE if it cannot be gotten.
        """

        return int(job_detail.get('container', {}).get('exitCode', EXIT_STATUS_UNAVAILABLE_VALUE))

    def getUpdatedBatchJob(self, maxWait: int) -> Optional[UpdatedBatchJobInfo]:
        # Remember when we started, for respecting the timeout
        entry = datetime.datetime.now()
        while ((datetime.datetime.now() - entry).total_seconds() < maxWait or not maxWait):
            result = self.getUpdatedLocalJob(0)
            if result:
                return result

            try:
                # Collect together the list of AWS and batch system IDs for tasks we
                # are acknowledging and don't care about anymore.
                acknowledged = []

                for job_detail in self._describe_jobs_in_batches():
                    if job_detail.get('status') in ['SUCCEEDED', 'FAILED']:
                        # This job is done!
                        aws_id = job_detail['jobId']
                        bs_id = self.aws_id_to_bs_id[aws_id]

                        # Acknowledge it
                        acknowledged.append((aws_id, bs_id))

                        if job_detail['jobId'] in self.killed_job_aws_ids:
                            # Killed jobs aren't allowed to appear as updated.
                            logger.debug('Job %s was killed so skipping it', bs_id)
                            continue

                        # Otherwise, it stopped running and it wasn't our fault.

                        # Record runtime
                        runtime = self._get_runtime(job_detail)

                        # Determine if it succeeded
                        exit_reason = STATE_TO_EXIT_REASON[job_detail['status']]

                        # Get its exit code
                        exit_code = self._get_exit_code(job_detail)

                        if job_detail['status'] == 'FAILED' and 'statusReason' in job_detail:
                            # AWS knows why the job failed, so log the error
                            logger.error('Job %s failed because: %s', bs_id, job_detail['statusReason'])

                        # Compose a result
                        return UpdatedBatchJobInfo(jobID=bs_id, exitStatus=exit_code, wallTime=runtime, exitReason=exit_reason)

            finally:
                # Drop all the records for tasks we acknowledged
                for (aws_id, bs_id) in acknowledged:
                    del self.aws_id_to_bs_id[aws_id]
                    del self.bs_id_to_aws_id[bs_id]
                    if aws_id in self.killed_job_aws_ids:
                        # We don't need to remember that we killed this job anymore.
                        self.killed_job_aws_ids.remove(aws_id)

            if maxWait:
                # Wait a bit and poll again
                time.sleep(min(maxWait/2, 1.0))
            else:
                # Only poll once
                break
        # If we get here we got nothing
        return None

    def shutdown(self) -> None:

        # Shutdown local processes first
        self.shutdownLocal()

        for aws_id in self.aws_id_to_bs_id.keys():
            # Shut down all the AWS jobs we issued.
            self._try_terminate(aws_id)

        # Get rid of the job definition we are using if we can.
        self._destroy_job_definition()

    @retry(errors=[BotoServerError])
    def _try_terminate(self, aws_id: str) -> None:
        """
        Internal function. Should not be called outside this class.

        Try to terminate an AWS Batch job.

        Succeed if it can't be canceled because it has stopped,
        but fail if it can't be canceled for some other reason.
        """
        # Remember that we killed this job so we don't show it as updated
        # later.
        self.killed_job_aws_ids.add(aws_id)
        # Kill the AWS Batch job
        self.client.terminate_job(jobId=aws_id, reason='Killed by Toil')

    @retry(errors=[BotoServerError])
    def _wait_until_stopped(self, aws_id: str) -> None:
        """
        Internal function. Should not be called outside this class.

        Wait for a terminated job to actually stop. The AWS Batch API does not
        guarantee that the status of a job will be SUCCEEDED or FAILED as soon
        as a terminate call succeeds for it, but Toil requires that a job that
        has been successfully killed can no longer be observed to be running.
        """

        while True:
            # Poll the job
            response = self.client.describe_jobs(jobs=[aws_id])
            jobs = response.get('jobs', [])
            if len(jobs) == 0:
                # Job no longer exists at all
                return
            job = jobs[0]
            if job.get('status') and job['status'] in ['SUCCEEDED', 'FAILED']:
                # The job has stopped
                return
            # Otherwise the job is still going. Wait for it to stop.
            logger.info('Waiting for killed job %s to stop', self.aws_id_to_bs_id.get(aws_id, aws_id))
            time.sleep(2)

    @retry(errors=[BotoServerError])
    def _get_or_create_job_definition(self) -> str:
        """
        Internal function. Should not be called outside this class.

        Create, if not already created, and return the ARN for the
        JobDefinition for this workflow run.
        """
        if self.job_definition is None:
            job_def_spec = {
                'jobDefinitionName': 'toil-' + str(uuid.uuid4()),
                'type': 'container',
                'containerProperties': {
                    'image': self.docker_image,
                    # Unlike the Kubernetes batch system we always mount the Toil
                    # workDir onto the host. Hopefully it has its ephemeral disks mounted there.
                    # TODO: Where do the default batch AMIs mount their ephemeral disks, if anywhere?
                    'volumes': [{'name': 'workdir', 'host': {'sourcePath': '/var/lib/toil'}}],
                    'mountPoints': [{'containerPath': self.worker_work_dir, 'sourceVolume': 'workdir'}],
                    # Requirements will always be overridden but must be present anyway
                    'resourceRequirements': [
                        {'type': 'MEMORY', 'value': str(max(MIN_REQUESTABLE_MIB, math.ceil(b_to_mib(self.config.defaultMemory))))},
                        {'type': 'VCPU', 'value': str(max(MIN_REQUESTABLE_CORES, math.ceil(self.config.defaultCores)))}
                    ]
                },
                'retryStrategy': {'attempts': 1},
                'propagateTags': True  # This will propagate to ECS task but not to job!
            }
            if self.job_role_arn:
                # We need to give the job a role.
                # We might not be able to do much job store access without this!
                container_properties = job_def_spec['containerProperties']
                assert isinstance(container_properties, dict)
                container_properties['jobRoleArn'] = self.job_role_arn
            if self.owner_tag:
                # We are meant to tag everything with an owner
                job_def_spec['tags'] = {'Owner': self.owner_tag}
            response = self.client.register_job_definition(**job_def_spec)
            self.job_definition = response['jobDefinitionArn']

        return self.job_definition

    @retry(errors=[BotoServerError])
    def _destroy_job_definition(self) -> None:
        """
        Internal function. Should not be called outside this class.

        Destroy any job definition we have created for this workflow run.
        """
        if self.job_definition is not None:
            self.client.deregister_job_definition(jobDefinition=self.job_definition)
            # TODO: How do we tolerate it not existing anymore?
            self.job_definition = None

    def getIssuedBatchJobIDs(self) -> List[int]:
        return self.getIssuedLocalJobIDs() + list(self.bs_id_to_aws_id.keys())

    def _describe_jobs_in_batches(self) -> Iterator[Dict[str, Any]]:
        """
        Internal function. Should not be called outside this class.

        Describe all the outstanding jobs in batches of a reasonable size.
        Yields each JobDetail.
        """

        # Get all the AWS IDs to poll
        to_check = list(aws_and_bs_id[0] for aws_and_bs_id in self.aws_id_to_bs_id.items())

        while len(to_check) > 0:
            # Go through jobs we want to poll in batches of the max size
            check_batch = to_check[-MAX_POLL_COUNT:]
            # And pop them off the end of the list of jobs to check
            to_check = to_check[:-len(check_batch)]

            # TODO: retry
            response = self.client.describe_jobs(jobs=check_batch)

            # Yield each returned JobDetail
            yield from response.get('jobs', [])

    def getRunningBatchJobIDs(self) -> Dict[int, float]:
        # We need a dict from job_id (integer) to seconds it has been running
        bs_id_to_runtime = {}

        for job_detail in self._describe_jobs_in_batches():
            if job_detail.get('status') == 'RUNNING':
                runtime = self._get_runtime(job_detail)
                aws_id = job_detail['jobId']
                bs_id = self.aws_id_to_bs_id[aws_id]
                if runtime:
                    # We can measure a runtime
                    bs_id_to_runtime[bs_id] = runtime
                else:
                    # If we can't find a runtime, we can't say it's running
                    # because we can't say how long it has been running for.
                    logger.warning("Job %s is %s but has no runtime: %s", bs_id, job_detail['status'], job_detail)

        # Give back the times all our running jobs have been running for.
        return bs_id_to_runtime

    def killBatchJobs(self, job_ids: List[int]) -> None:
        # Kill all the ones that are local
        self.killLocalJobs(job_ids)

        for bs_id in job_ids:
            if bs_id in self.bs_id_to_aws_id:
                # We sent this to AWS Batch. So try to cancel it.
                self._try_terminate(self.bs_id_to_aws_id[bs_id])
                # But don't forget the mapping until we actually get the finish
                # notification for the job.
        for bs_id in job_ids:
            if bs_id in self.bs_id_to_aws_id:
                # Poll each job to make sure it is dead and won't look running,
                # before we return. TODO: we could do this in batches to save
                # requests, but we already make O(n) requests to issue the kills.
                self._wait_until_stopped(self.bs_id_to_aws_id[bs_id])

    @classmethod
    def add_options(cls, parser: Union[ArgumentParser, _ArgumentGroup]) -> None:
        parser.add_argument("--awsBatchRegion", dest="aws_batch_region", default=None,
                            help="The AWS region containing the AWS Batch queue to submit to.")
        parser.add_argument("--awsBatchQueue", dest="aws_batch_queue", default=None,
                            help="The name or ARN of the AWS Batch queue to submit to.")
        parser.add_argument("--awsBatchJobRoleArn", dest="aws_batch_job_role_arn", default=None,
                            help=("The ARN of an IAM role to run AWS Batch jobs as, so they "
                                  "can e.g. access a job store. Must be assumable by "
                                  "ecs-tasks.amazonaws.com."))

    @classmethod
    def setOptions(cls, setOption: Callable[..., None]) -> None:
        setOption("aws_batch_region", default=None, env=["TOIL_AWS_BATCH_REGION"])
        setOption("aws_batch_queue", default=None, env=["TOIL_AWS_BATCH_QUEUE"])
        setOption("aws_batch_job_role_arn", default=None, env=["TOIL_AWS_BATCH_JOB_ROLE_ARN"])
