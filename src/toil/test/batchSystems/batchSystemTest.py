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
import fcntl
import itertools
import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from abc import ABCMeta, abstractmethod
from fractions import Fraction
from unittest import skipIf

from toil.batchSystems.abstractBatchSystem import (AbstractBatchSystem,
                                                   BatchSystemSupport,
                                                   InsufficientSystemResources)
# Don't import any batch systems here that depend on extras
# in order to import properly. Import them later, in tests
# protected by annotations.
from toil.batchSystems.mesos.test import MesosTestSupport
from toil.batchSystems.parasol import ParasolBatchSystem
from toil.batchSystems.registry import (
    BATCH_SYSTEM_FACTORY_REGISTRY,
    BATCH_SYSTEMS,
    addBatchSystemFactory,
    restore_batch_system_plugin_state,
    save_batch_system_plugin_state,
)
from toil.batchSystems.singleMachine import SingleMachineBatchSystem
from toil.common import Config, Toil
from toil.job import Job, JobDescription, Requirer
from toil.lib.retry import retry_flaky_test
from toil.lib.threading import cpu_count
from toil.test import (ToilTest,
                       needs_aws_batch,
                       needs_aws_s3,
                       needs_fetchable_appliance,
                       needs_gridengine,
                       needs_htcondor,
                       needs_kubernetes_installed,
                       needs_kubernetes,
                       needs_lsf,
                       needs_mesos,
                       needs_parasol,
                       needs_slurm,
                       needs_tes,
                       needs_torque,
                       slow)
from toil.test.batchSystems.parasolTestSupport import ParasolTestSupport

logger = logging.getLogger(__name__)

# How many cores should be utilized by this test. The test will fail if the running system
# doesn't have at least that many cores.

numCores = 2

preemptable = False

# Since we aren't always attaching the config to the jobs for these tests, we
# need to use fully specified requirements.
defaultRequirements = dict(memory=int(100e6), cores=1, disk=1000, preemptable=preemptable, accelerators=[])

class BatchSystemPluginTest(ToilTest):
    """
    Class for testing batch system plugin functionality.
    """

    def setUp(self):
        # Save plugin state so our plugin doesn't stick around after the test
        # (and create duplicate options)
        self.__state = save_batch_system_plugin_state()
        super().setUp()

    def tearDown(self):
        # Restore plugin state
        restore_batch_system_plugin_state(self.__state)
        super().tearDown()

    def testAddBatchSystemFactory(self):
        def test_batch_system_factory():
            # TODO: Adding the same batch system under multiple names means we
            # can't actually create Toil options, because each version tries to
            # add its arguments.
            return SingleMachineBatchSystem

        addBatchSystemFactory('testBatchSystem', test_batch_system_factory)
        assert ('testBatchSystem', test_batch_system_factory) in BATCH_SYSTEM_FACTORY_REGISTRY.items()
        assert 'testBatchSystem' in BATCH_SYSTEMS

class hidden:
    """
    Hide abstract base class from unittest's test case loader

    http://stackoverflow.com/questions/1323455/python-unit-test-with-base-and-sub-class#answer-25695512
    """

    class AbstractBatchSystemTest(ToilTest, metaclass=ABCMeta):
        """
        A base test case with generic tests that every batch system should pass.

        Cannot assume that the batch system actually executes commands on the local machine/filesystem.
        """

        @abstractmethod
        def createBatchSystem(self) -> AbstractBatchSystem:
            raise NotImplementedError

        def supportsWallTime(self):
            return False

        @classmethod
        def createConfig(cls):
            """
            Returns a dummy config for the batch system tests.  We need a workflowID to be set up
            since we are running tests without setting up a jobstore. This is the class version
            to be used when an instance is not available.

            :rtype: toil.common.Config
            """
            config = Config()
            from uuid import uuid4
            config.workflowID = str(uuid4())
            config.cleanWorkDir = 'always'
            return config

        def _createConfig(self):
            """
            Returns a dummy config for the batch system tests.  We need a workflowID to be set up
            since we are running tests without setting up a jobstore.

            :rtype: toil.common.Config
            """
            return self.createConfig()

        def _mockJobDescription(self, jobStoreID=None, command=None, **kwargs):
            """
            Create a mock-up JobDescription with the given ID, command, and other parameters.
            """

            # TODO: Use a real unittest.Mock? For now we make a real instance and just hack it up.

            desc = JobDescription(**kwargs)
            # Normally we can't pass in a command or ID, and the job
            # serialization logic takes care of filling them in. We set them
            # here.
            if command is not None:
                desc.command = command
            if jobStoreID is not None:
                desc.jobStoreID = jobStoreID

            return desc

        @classmethod
        def setUpClass(cls):
            super(hidden.AbstractBatchSystemTest, cls).setUpClass()
            logging.basicConfig(level=logging.DEBUG)

        def setUp(self):
            super(hidden.AbstractBatchSystemTest, self).setUp()
            self.config = self._createConfig()
            self.batchSystem = self.createBatchSystem()
            self.tempDir = self._createTempDir('testFiles')

        def tearDown(self):
            self.batchSystem.shutdown()
            super(hidden.AbstractBatchSystemTest, self).tearDown()

        def get_max_startup_seconds(self) -> int:
            """
            Get the number of seconds this test ought to wait for the first job to run.
            Some batch systems may need time to scale up.
            """
            return 120

        def test_available_cores(self):
            self.assertTrue(cpu_count() >= numCores)

        @retry_flaky_test(prepare=[tearDown, setUp])
        def test_run_jobs(self):
            jobDesc1 = self._mockJobDescription(command='sleep 1000', jobName='test1', unitName=None,
                                                jobStoreID='1', requirements=defaultRequirements)
            jobDesc2 = self._mockJobDescription(command='sleep 1000', jobName='test2', unitName=None,
                                                jobStoreID='2', requirements=defaultRequirements)
            job1 = self.batchSystem.issueBatchJob(jobDesc1)
            job2 = self.batchSystem.issueBatchJob(jobDesc2)

            issuedIDs = self._waitForJobsToIssue(2)
            self.assertEqual(set(issuedIDs), {job1, job2})

            # Now at some point we want these jobs to become running
            # But since we may be testing against a live cluster (Kubernetes)
            # we want to handle weird cases and high cluster load as much as we can.

            # Wait a bit for any Dockers to download and for the
            # jobs to have a chance to start.
            # TODO: We insist on neither of these ever finishing when we test
            # getUpdatedBatchJob, and the sleep time is longer than the time we
            # should spend waiting for both to start, so if our cluster can
            # only run one job at a time, we will fail the test.
            runningJobIDs = self._waitForJobsToStart(2, tries=self.get_max_startup_seconds())
            self.assertEqual(set(runningJobIDs), {job1, job2})

            # Killing the jobs instead of allowing them to complete means this test can run very
            # quickly if the batch system issues and starts the jobs quickly.
            self.batchSystem.killBatchJobs([job1, job2])
            self.assertEqual({}, self.batchSystem.getRunningBatchJobIDs())

            # Issue a job and then allow it to finish by itself, causing it to be added to the
            # updated jobs queue.
            # We would like to have this touch something on the filesystem and
            # then check for it having happened, but we can't guarantee that
            # the batch system will run against the same filesystem we are
            # looking at.
            jobDesc3 = self._mockJobDescription(command="mktemp -d", jobName='test3', unitName=None,
                                                jobStoreID='3', requirements=defaultRequirements)
            job3 = self.batchSystem.issueBatchJob(jobDesc3)

            jobUpdateInfo = self.batchSystem.getUpdatedBatchJob(maxWait=1000)
            jobID, exitStatus, wallTime = jobUpdateInfo.jobID, jobUpdateInfo.exitStatus, jobUpdateInfo.wallTime
            logger.info(f'Third job completed: {jobID} {exitStatus} {wallTime}')

            # Since the first two jobs were killed, the only job in the updated jobs queue should
            # be job 3. If the first two jobs were (incorrectly) added to the queue, this will
            # fail with jobID being equal to job1 or job2.
            self.assertEqual(jobID, job3)
            self.assertEqual(exitStatus, 0)
            if self.supportsWallTime():
                self.assertTrue(wallTime > 0)
            else:
                self.assertIsNone(wallTime)
            # TODO: Work out a way to check if the job we asked to run actually ran.
            # Don't just believe the batch system, but don't assume it ran on this machine either.
            self.assertFalse(self.batchSystem.getUpdatedBatchJob(0))

            # Make sure killBatchJobs can handle jobs that don't exist
            self.batchSystem.killBatchJobs([10])

        def test_set_env(self):
            # Parasol disobeys shell rules and splits the command at the space
            # character into arguments before exec'ing it, whether the space is
            # quoted, escaped or not.

            # Start with a relatively safe script
            script_shell = 'if [ "x${FOO}" == "xbar" ] ; then exit 23 ; else exit 42 ; fi'

            # Escape the semicolons
            script_protected = script_shell.replace(';', r'\;')

            # Turn into a string which convinces bash to take all args and paste them back together and run them
            command = "bash -c \"\\${@}\" bash eval " + script_protected
            jobDesc4 = self._mockJobDescription(command=command, jobName='test4', unitName=None,
                                                jobStoreID='4', requirements=defaultRequirements)
            job4 = self.batchSystem.issueBatchJob(jobDesc4)
            jobUpdateInfo = self.batchSystem.getUpdatedBatchJob(maxWait=1000)
            jobID, exitStatus, wallTime = jobUpdateInfo.jobID, jobUpdateInfo.exitStatus, jobUpdateInfo.wallTime
            self.assertEqual(exitStatus, 42)
            self.assertEqual(jobID, job4)
            # Now set the variable and ensure that it is present
            self.batchSystem.setEnv('FOO', 'bar')
            jobDesc5 = self._mockJobDescription(command=command, jobName='test5', unitName=None,
                                                jobStoreID='5', requirements=defaultRequirements)
            job5 = self.batchSystem.issueBatchJob(jobDesc5)
            jobUpdateInfo = self.batchSystem.getUpdatedBatchJob(maxWait=1000)
            self.assertEqual(jobUpdateInfo.exitStatus, 23)
            self.assertEqual(jobUpdateInfo.jobID, job5)

        def test_set_job_env(self):
            """ Test the mechanism for setting per-job environment variables to batch system jobs."""
            script = 'if [ "x${FOO}" == "xbar" ] ; then exit 23 ; else exit 42 ; fi'
            command = "bash -c \"\\${@}\" bash eval " + script.replace(';', r'\;')

            # Issue a job with a job environment variable
            job_desc_6 = self._mockJobDescription(command=command, jobName='test6', unitName=None,
                                                  jobStoreID='6', requirements=defaultRequirements)
            job6 = self.batchSystem.issueBatchJob(job_desc_6, job_environment={
                'FOO': 'bar'
            })
            job_update_info = self.batchSystem.getUpdatedBatchJob(maxWait=1000)
            self.assertEqual(job_update_info.exitStatus, 23)  # this should succeed
            self.assertEqual(job_update_info.jobID, job6)
            # Now check that the environment variable doesn't exist for other jobs
            job_desc_7 = self._mockJobDescription(command=command, jobName='test7', unitName=None,
                                                  jobStoreID='7', requirements=defaultRequirements)
            job7 = self.batchSystem.issueBatchJob(job_desc_7)
            job_update_info = self.batchSystem.getUpdatedBatchJob(maxWait=1000)
            self.assertEqual(job_update_info.exitStatus, 42)
            self.assertEqual(job_update_info.jobID, job7)

        def testCheckResourceRequest(self):
            if isinstance(self.batchSystem, BatchSystemSupport):
                check_resource_request = self.batchSystem.check_resource_request
                # Assuming we have <2000 cores, this should be too many cores
                self.assertRaises(InsufficientSystemResources, check_resource_request,
                                  Requirer(dict(memory=1000, cores=2000, disk='1G', accelerators=[])))
                self.assertRaises(InsufficientSystemResources, check_resource_request,
                                  Requirer(dict(memory=5, cores=2000, disk='1G', accelerators=[])))

                # This should be too much memory
                self.assertRaises(InsufficientSystemResources, check_resource_request,
                                  Requirer(dict(memory='5000G', cores=1, disk='1G', accelerators=[])))

                # This should be too much disk
                self.assertRaises(InsufficientSystemResources, check_resource_request,
                                  Requirer(dict(memory=5, cores=1, disk='2G', accelerators=[])))

                # This should be an accelerator we don't have.
                # All the batch systems need code to know they don't have these accelerators.
                self.assertRaises(InsufficientSystemResources, check_resource_request,
                                  Requirer(dict(memory=5, cores=1, disk=100, accelerators=[{'kind': 'turbo-encabulator', 'count': 1}])))

                # These should be missing attributes
                self.assertRaises(AttributeError, check_resource_request,
                                  Requirer(dict(memory=5, cores=1, disk=1000)))
                self.assertRaises(AttributeError, check_resource_request,
                                  Requirer(dict(cores=1, disk=1000, accelerators=[])))
                self.assertRaises(AttributeError, check_resource_request,
                                  Requirer(dict(memory=10, disk=1000, accelerators=[])))

                # This should actually work
                check_resource_request(Requirer(dict(memory=10, cores=1, disk=100, accelerators=[])))

        def testScalableBatchSystem(self):
            # If instance of scalable batch system
            pass

        def _waitForJobsToIssue(self, numJobs):
            issuedIDs = []
            for it in range(20):
                issuedIDs = self.batchSystem.getIssuedBatchJobIDs()
                if len(issuedIDs) == numJobs:
                    break
                time.sleep(1)
            return issuedIDs

        def _waitForJobsToStart(self, numJobs, tries=20):
            """
            Loop until the given number of distinct jobs are in the
            running state, or until the given number of tries is exhausted
            (with 1 second polling period).

            Returns the list of IDs that are running.
            """
            runningIDs = []
            # prevent an endless loop, give it a few tries
            for it in range(tries):
                running = self.batchSystem.getRunningBatchJobIDs()
                logger.info(f'Running jobs now: {running}')
                runningIDs = list(running.keys())
                if len(runningIDs) == numJobs:
                    break
                time.sleep(1)
            return runningIDs

    class AbstractBatchSystemJobTest(ToilTest, metaclass=ABCMeta):
        """
        An abstract base class for batch system tests that use a full Toil workflow rather
        than using the batch system directly.
        """

        cpuCount = cpu_count()
        allocatedCores = sorted({1, 2, cpuCount})
        sleepTime = 5

        @abstractmethod
        def getBatchSystemName(self):
            """
            :rtype: (str, AbstractBatchSystem)
            """
            raise NotImplementedError

        def getOptions(self, tempDir):
            """
            Configures options for Toil workflow and makes job store.
            :param str tempDir: path to test directory
            :return: Toil options object
            """
            options = Job.Runner.getDefaultOptions(self._getTestJobStorePath())
            options.logLevel = "DEBUG"
            options.batchSystem = self.batchSystemName
            options.workDir = tempDir
            options.maxCores = self.cpuCount
            return options

        def setUp(self):
            self.batchSystemName = self.getBatchSystemName()
            super(hidden.AbstractBatchSystemJobTest, self).setUp()

        def tearDown(self):
            super(hidden.AbstractBatchSystemJobTest, self).tearDown()

        @slow
        def testJobConcurrency(self):
            """
            Tests that the batch system is allocating core resources properly for concurrent tasks.
            """
            for coresPerJob in self.allocatedCores:
                tempDir = self._createTempDir('testFiles')
                options = self.getOptions(tempDir)

                counterPath = os.path.join(tempDir, 'counter')
                resetCounters(counterPath)
                value, maxValue = getCounters(counterPath)
                assert (value, maxValue) == (0, 0)

                root = Job()
                for _ in range(self.cpuCount):
                    root.addFollowOn(Job.wrapFn(measureConcurrency, counterPath, self.sleepTime,
                                                cores=coresPerJob, memory='1M', disk='1Mi'))
                with Toil(options) as toil:
                    toil.start(root)
                _, maxValue = getCounters(counterPath)
                self.assertEqual(maxValue, self.cpuCount // coresPerJob)

        def test_omp_threads(self):
            """
            Test if the OMP_NUM_THREADS env var is set correctly based on jobs.cores.
            """
            test_cases = {
                # mapping of the number of cores to the OMP_NUM_THREADS value
                0.1: "1",
                1: "1",
                2: "2"
            }

            temp_dir = self._createTempDir()
            options = self.getOptions(temp_dir)

            for cores, expected_omp_threads in test_cases.items():
                if os.environ.get('OMP_NUM_THREADS'):
                    expected_omp_threads = os.environ.get('OMP_NUM_THREADS')
                    logger.info(f"OMP_NUM_THREADS is set.  Using OMP_NUM_THREADS={expected_omp_threads} instead.")
                with Toil(options) as toil:
                    output = toil.start(Job.wrapFn(get_omp_threads, memory='1Mi', cores=cores, disk='1Mi'))
                self.assertEqual(output, expected_omp_threads)

    class AbstractGridEngineBatchSystemTest(AbstractBatchSystemTest):
        """
        An abstract class to reduce redundancy between Grid Engine, Slurm, and other similar batch
        systems
        """

        def _createConfig(self):
            config = super(hidden.AbstractGridEngineBatchSystemTest, self)._createConfig()
            config.statePollingWait = 0.5  # Reduce polling wait so tests run faster
            # can't use _getTestJobStorePath since that method removes the directory
            config.jobStore = 'file:' + self._createTempDir('jobStore')
            return config

@needs_kubernetes
@needs_aws_s3
@needs_fetchable_appliance
class KubernetesBatchSystemTest(hidden.AbstractBatchSystemTest):
    """
    Tests against the Kubernetes batch system
    """

    def supportsWallTime(self):
        return True

    def createBatchSystem(self):
        # We know we have Kubernetes so we can import the batch system
        from toil.batchSystems.kubernetes import KubernetesBatchSystem
        return KubernetesBatchSystem(config=self.config,
                                     maxCores=numCores, maxMemory=1e9, maxDisk=2001)

@needs_kubernetes_installed
class KubernetesBatchSystemBenchTest(ToilTest):
    """
    Kubernetes batch system unit tests that don't need to actually talk to a cluster.
    """

    def test_preemptability_constraints(self):
        """
        Make sure we generate the right preemptability constraints.
        """

        # Make sure we can print diffs of these long strings
        self.maxDiff = 10000

        from kubernetes.client import V1PodSpec
        from toil.batchSystems.kubernetes import KubernetesBatchSystem

        normal_spec = V1PodSpec(containers=[])
        constraints = KubernetesBatchSystem.Placement()
        constraints.set_preemptable(False)
        constraints.apply(normal_spec)
        self.assertEqual(textwrap.dedent("""
        {'node_affinity': {'preferred_during_scheduling_ignored_during_execution': None,
                           'required_during_scheduling_ignored_during_execution': {'node_selector_terms': [{'match_expressions': [{'key': 'eks.amazonaws.com/capacityType',
                                                                                                                                   'operator': 'NotIn',
                                                                                                                                   'values': ['SPOT']},
                                                                                                                                  {'key': 'cloud.google.com/gke-preemptible',
                                                                                                                                   'operator': 'NotIn',
                                                                                                                                   'values': ['true']}],
                                                                                                            'match_fields': None}]}},
         'pod_affinity': None,
         'pod_anti_affinity': None}
        """).strip(), str(normal_spec.affinity))
        self.assertEqual(str(normal_spec.tolerations), "None")

        spot_spec = V1PodSpec(containers=[])
        constraints = KubernetesBatchSystem.Placement()
        constraints.set_preemptable(True)
        constraints.apply(spot_spec)
        self.assertEqual(textwrap.dedent("""
        {'node_affinity': {'preferred_during_scheduling_ignored_during_execution': [{'preference': {'match_expressions': [{'key': 'eks.amazonaws.com/capacityType',
                                                                                                                           'operator': 'In',
                                                                                                                           'values': ['SPOT']}],
                                                                                                    'match_fields': None},
                                                                                     'weight': 1},
                                                                                    {'preference': {'match_expressions': [{'key': 'cloud.google.com/gke-preemptible',
                                                                                                                           'operator': 'In',
                                                                                                                           'values': ['true']}],
                                                                                                    'match_fields': None},
                                                                                     'weight': 1}],
                           'required_during_scheduling_ignored_during_execution': None},
         'pod_affinity': None,
         'pod_anti_affinity': None}
        """).strip(), str(spot_spec.affinity), )
        self.assertEqual(textwrap.dedent("""
        [{'effect': None,
         'key': 'cloud.google.com/gke-preemptible',
         'operator': None,
         'toleration_seconds': None,
         'value': 'true'}]
        """).strip(), str(spot_spec.tolerations))
        
    def test_label_constraints(self):
        """
        Make sure we generate the right preemptability constraints.
        """

        # Make sure we can print diffs of these long strings
        self.maxDiff = 10000

        from kubernetes.client import V1PodSpec
        from toil.batchSystems.kubernetes import KubernetesBatchSystem

        spec = V1PodSpec(containers=[])
        constraints = KubernetesBatchSystem.Placement()
        constraints.required_labels = [('GottaBeSetTo', ['This'])]
        constraints.desired_labels = [('OutghtToBeSetTo', ['That'])]
        constraints.prohibited_labels = [('CannotBe', ['ABadThing'])]
        constraints.apply(spec)
        self.assertEqual(textwrap.dedent("""
        {'node_affinity': {'preferred_during_scheduling_ignored_during_execution': [{'preference': {'match_expressions': [{'key': 'OutghtToBeSetTo',
                                                                                                                           'operator': 'In',
                                                                                                                           'values': ['That']}],
                                                                                                    'match_fields': None},
                                                                                     'weight': 1}],
                           'required_during_scheduling_ignored_during_execution': {'node_selector_terms': [{'match_expressions': [{'key': 'GottaBeSetTo',
                                                                                                                                   'operator': 'In',
                                                                                                                                   'values': ['This']},
                                                                                                                                  {'key': 'CannotBe',
                                                                                                                                   'operator': 'NotIn',
                                                                                                                                   'values': ['ABadThing']}],
                                                                                                            'match_fields': None}]}},
         'pod_affinity': None,
         'pod_anti_affinity': None}
        """).strip(), str(spec.affinity),)
        self.assertEqual(str(spec.tolerations), "None")


@needs_tes
@needs_fetchable_appliance
class TESBatchSystemTest(hidden.AbstractBatchSystemTest):
    """
    Tests against the TES batch system
    """

    def supportsWallTime(self):
        return True

    def createBatchSystem(self):
        # Import the batch system when we know we have it.
        # Doesn't really matter for TES right now, but someday it might.
        from toil.batchSystems.tes import TESBatchSystem
        return TESBatchSystem(config=self.config,
                              maxCores=numCores, maxMemory=1e9, maxDisk=2001)

@needs_aws_batch
@needs_fetchable_appliance
class AWSBatchBatchSystemTest(hidden.AbstractBatchSystemTest):
    """
    Tests against the AWS Batch batch system
    """

    def supportsWallTime(self):
        return True

    def createBatchSystem(self):
        from toil.batchSystems.awsBatch import AWSBatchBatchSystem
        return AWSBatchBatchSystem(config=self.config,
                                   maxCores=numCores, maxMemory=1e9, maxDisk=2001)

    def get_max_startup_seconds(self) -> int:
        # AWS Batch may need to scale out the compute environment.
        return 300

@slow
@needs_mesos
class MesosBatchSystemTest(hidden.AbstractBatchSystemTest, MesosTestSupport):
    """
    Tests against the Mesos batch system
    """

    @classmethod
    def createConfig(cls):
        """
        needs to set mesos_endpoint to localhost for testing since the default is now the
        private IP address
        """
        config = super().createConfig()
        config.mesos_endpoint = 'localhost:5050'
        return config

    def supportsWallTime(self):
        return True

    def createBatchSystem(self):
        # We know we have Mesos so we can import the batch system
        from toil.batchSystems.mesos.batchSystem import MesosBatchSystem
        self._startMesos(numCores)
        return MesosBatchSystem(config=self.config,
                                maxCores=numCores, maxMemory=1e9, maxDisk=1001)

    def tearDown(self):
        self._stopMesos()
        super().tearDown()

    def testIgnoreNode(self):
        self.batchSystem.ignoreNode('localhost')
        jobDesc = self._mockJobDescription(command='sleep 1000', jobName='test2', unitName=None,
                                           jobStoreID='1', requirements=defaultRequirements)
        job = self.batchSystem.issueBatchJob(jobDesc)

        issuedID = self._waitForJobsToIssue(1)
        self.assertEqual(set(issuedID), {job})

        # Wait until a job starts or we go a while without that happening
        runningJobIDs = self._waitForJobsToStart(1, tries=20)
        # Make sure job is NOT running
        self.assertEqual(set(runningJobIDs), set({}))


def write_temp_file(s: str, temp_dir: str) -> str:
    """
    Dump a string into a temp file and return its path.
    """
    fd, path = tempfile.mkstemp(dir=temp_dir)
    try:
        encoded = s.encode('utf-8')
        assert os.write(fd, encoded) == len(encoded)
    except:
        os.unlink(path)
        raise
    else:
        return path
    finally:
        os.close(fd)


class SingleMachineBatchSystemTest(hidden.AbstractBatchSystemTest):
    """
    Tests against the single-machine batch system
    """

    def supportsWallTime(self) -> bool:
        return True

    def createBatchSystem(self) -> AbstractBatchSystem:
        return SingleMachineBatchSystem(config=self.config,
                                        maxCores=numCores, maxMemory=1e9, maxDisk=2001)

    def testProcessEscape(self, hide: bool = False) -> None:
        """
        Test to make sure that child processes and their descendants go away
        when the Toil workflow stops.

        If hide is true, will try and hide the child processes to make them
        hard to stop.
        """

        def script() -> None:
            #!/usr/bin/env python3
            import fcntl
            import os
            import signal
            import sys
            import time
            from typing import Any

            def handle_signal(sig: Any, frame: Any) -> None:
                sys.stderr.write(f'{os.getpid()} ignoring signal {sig}\n')

            if hasattr(signal, 'valid_signals'):
                # We can just ask about the signals
                all_signals = signal.valid_signals()
            else:
                # Fish them out by name
                all_signals = [getattr(signal, n) for n in dir(signal) if n.startswith('SIG') and not n.startswith('SIG_')]

            for sig in all_signals:
                # Set up to ignore all signals we can and generally be obstinate
                if sig != signal.SIGKILL and sig != signal.SIGSTOP:
                    signal.signal(sig, handle_signal)

            if len(sys.argv) > 2:
                # Instructed to hide
                if os.fork():
                    # Try and hide the first process immediately so getting its
                    # pgid won't work.
                    sys.exit(0)

            for depth in range(3):
                # Bush out into a tree of processes
                os.fork()

            if len(sys.argv) > 1:
                fd = os.open(sys.argv[1], os.O_RDONLY)
                fcntl.lockf(fd, fcntl.LOCK_SH)

            sys.stderr.write(f'{os.getpid()} waiting...\n')

            while True:
                # Wait around forever
                time.sleep(60)

        # Get a directory where we can safely dump files.
        temp_dir = self._createTempDir()

        script_path = write_temp_file(self._getScriptSource(script), temp_dir)

        # We will have all the job processes try and lock this file shared while they are alive.
        lockable_path = write_temp_file('', temp_dir)

        try:
            command = f'{sys.executable} {script_path} {lockable_path}'
            if hide:
                # Tell the children to stop the first child and hide out in the
                # process group it made.
                command += ' hide'

            # Start the job
            self.batchSystem.issueBatchJob(self._mockJobDescription(command=command, jobName='fork',
                                                                    jobStoreID='1', requirements=defaultRequirements))
            # Wait
            time.sleep(10)

            lockfile = open(lockable_path, 'w')

            if not hide:
                # In hiding mode the job will finish, and the batch system will
                # clean up after it promptly. In non-hiding mode the job will
                # stick around until shutdown, so make sure we can see it.

                # Try to lock the file and make sure it fails

                try:
                    fcntl.lockf(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    assert False, "Should not be able to lock file while job is running"
                except OSError:
                    pass

            # Shut down the batch system
            self.batchSystem.shutdown()

            # After the batch system shuts down, we should be able to get the
            # lock immediately, because all the children should be gone.
            fcntl.lockf(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Then we can release it
            fcntl.lockf(lockfile, fcntl.LOCK_UN)
        finally:
            os.unlink(script_path)
            os.unlink(lockable_path)

    def testHidingProcessEscape(self):
        """
        Test to make sure that child processes and their descendants go away
        when the Toil workflow stops, even if the job process stops and leaves children.
        """

        self.testProcessEscape(hide=True)


@slow
class MaxCoresSingleMachineBatchSystemTest(ToilTest):
    """
    This test ensures that single machine batch system doesn't exceed the configured number
    cores
    """

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        logging.basicConfig(level=logging.DEBUG)

    def setUp(self) -> None:
        super().setUp()

        temp_dir = self._createTempDir()

        # Write initial value of counter file containing a tuple of two integers (i, n) where i
        # is the number of currently executing tasks and n the maximum observed value of i
        self.counterPath = write_temp_file('0,0', temp_dir)

        def script() -> None:
            import fcntl
            import os
            import sys
            import time
            def count(delta: int) -> None:
                """
                Adjust the first integer value in a file by the given amount. If the result
                exceeds the second integer value, set the second one to the first.
                """
                fd = os.open(sys.argv[1], os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    try:
                        s = os.read(fd, 10).decode('utf-8')
                        value, maxValue = list(map(int, s.split(',')))
                        value += delta
                        if value > maxValue: maxValue = value
                        os.lseek(fd, 0, 0)
                        os.ftruncate(fd, 0)
                        os.write(fd, f'{value},{maxValue}'.encode('utf-8'))
                    finally:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

            # Without the second argument, increment counter, sleep one second and decrement.
            # Othwerise, adjust the counter by the given delta, which can be useful for services.
            if len(sys.argv) < 3:
                count(1)
                try:
                    time.sleep(1)
                finally:
                    count(-1)
            else:
                count(int(sys.argv[2]))

        self.scriptPath = write_temp_file(self._getScriptSource(script), temp_dir)

    def tearDown(self) -> None:
        os.unlink(self.scriptPath)
        os.unlink(self.counterPath)

    def scriptCommand(self) -> str:
        return ' '.join([sys.executable, self.scriptPath, self.counterPath])

    @retry_flaky_test(prepare=[tearDown, setUp])
    def test(self):
        # We'll use fractions to avoid rounding errors. Remember that not every fraction can be
        # represented as a floating point number.
        F = Fraction
        # This test isn't general enough to cover every possible value of minCores in
        # SingleMachineBatchSystem. Instead we hard-code a value and assert it.
        minCores = F(1, 10)
        self.assertEqual(float(minCores), SingleMachineBatchSystem.minCores)
        for maxCores in {F(minCores), minCores * 10, F(1), F(numCores, 2), F(numCores)}:
            for coresPerJob in {F(minCores), F(minCores * 10), F(1), F(maxCores, 2), F(maxCores)}:
                for load in (F(1, 10), F(1), F(10)):
                    jobs = int(maxCores / coresPerJob * load)
                    if jobs >= 1 and minCores <= coresPerJob < maxCores:
                        self.assertEqual(maxCores, float(maxCores))
                        bs = SingleMachineBatchSystem(
                            config=hidden.AbstractBatchSystemTest.createConfig(),
                            maxCores=float(maxCores),
                            # Ensure that memory or disk requirements don't get in the way.
                            maxMemory=jobs * 10,
                            maxDisk=jobs * 10)
                        try:
                            jobIds = set()
                            for i in range(0, int(jobs)):
                                jobIds.add(bs.issueBatchJob(JobDescription(command=self.scriptCommand(),
                                                                           requirements=dict(
                                                                               cores=float(coresPerJob),
                                                                               memory=1, disk=1,
                                                                               accelerators=[],
                                                                               preemptable=preemptable),
                                                                           jobName=str(i), unitName='')))
                            self.assertEqual(len(jobIds), jobs)
                            while jobIds:
                                job = bs.getUpdatedBatchJob(maxWait=10)
                                self.assertIsNotNone(job)
                                jobId, status, wallTime = job.jobID, job.exitStatus, job.wallTime
                                self.assertEqual(status, 0)
                                # would raise KeyError on absence
                                jobIds.remove(jobId)
                        finally:
                            bs.shutdown()
                        concurrentTasks, maxConcurrentTasks = getCounters(self.counterPath)
                        self.assertEqual(concurrentTasks, 0)
                        logger.info('maxCores: {maxCores}, '
                                 'coresPerJob: {coresPerJob}, '
                                 'load: {load}'.format(**locals()))
                        # This is the key assertion:
                        expectedMaxConcurrentTasks = min(maxCores // coresPerJob, jobs)
                        self.assertEqual(maxConcurrentTasks, expectedMaxConcurrentTasks)
                        resetCounters(self.counterPath)

    @skipIf(SingleMachineBatchSystem.numCores < 3, 'Need at least three cores to run this test')
    def testServices(self):
        options = Job.Runner.getDefaultOptions(self._getTestJobStorePath())
        options.logDebug = True
        options.maxCores = 3
        self.assertTrue(options.maxCores <= SingleMachineBatchSystem.numCores)
        Job.Runner.startToil(Job.wrapJobFn(parentJob, self.scriptCommand()), options)
        with open(self.counterPath, 'r+') as f:
            s = f.read()
        logger.info('Counter is %s', s)
        self.assertEqual(getCounters(self.counterPath), (0, 3))


# Toil can use only top-level functions so we have to add them here:

def parentJob(job, cmd):
    job.addChildJobFn(childJob, cmd)


def childJob(job, cmd):
    job.addService(Service(cmd))
    job.addChildJobFn(grandChildJob, cmd)
    subprocess.check_call(cmd, shell=True)


def grandChildJob(job, cmd):
    job.addService(Service(cmd))
    job.addChildFn(greatGrandChild, cmd)
    subprocess.check_call(cmd, shell=True)


def greatGrandChild(cmd):
    subprocess.check_call(cmd, shell=True)


class Service(Job.Service):
    def __init__(self, cmd):
        super().__init__()
        self.cmd = cmd

    def start(self, fileStore):
        subprocess.check_call(self.cmd + ' 1', shell=True)

    def check(self):
        return True

    def stop(self, fileStore):
        subprocess.check_call(self.cmd + ' -1', shell=True)


@slow
@needs_parasol
class ParasolBatchSystemTest(hidden.AbstractBatchSystemTest, ParasolTestSupport):
    """
    Tests the Parasol batch system
    """

    def supportsWallTime(self):
        return True

    def _createConfig(self):
        config = super()._createConfig()
        # can't use _getTestJobStorePath since that method removes the directory
        config.jobStore = self._createTempDir('jobStore')
        return config

    def createBatchSystem(self) -> AbstractBatchSystem:
        memory = int(3e9)
        self._startParasol(numCores=numCores, memory=memory)

        return ParasolBatchSystem(config=self.config,
                                  maxCores=numCores,
                                  maxMemory=memory,
                                  maxDisk=1001)

    def tearDown(self):
        super().tearDown()
        self._stopParasol()

    def testBatchResourceLimits(self):
        jobDesc1 = JobDescription(command="sleep 1000",
                                  requirements=dict(memory=1 << 30, cores=1,
                                                    disk=1000, accelerators=[],
                                                    preemptable=preemptable),
                                  jobName='testResourceLimits')
        job1 = self.batchSystem.issueBatchJob(jobDesc1)
        self.assertIsNotNone(job1)
        jobDesc2 = JobDescription(command="sleep 1000",
                                  requirements=dict(memory=2 << 30, cores=1,
                                                    disk=1000, accelerators=[],
                                                    preemptable=preemptable),
                                  jobName='testResourceLimits')
        job2 = self.batchSystem.issueBatchJob(jobDesc2)
        self.assertIsNotNone(job2)
        batches = self._getBatchList()
        self.assertEqual(len(batches), 2)
        # It would be better to directly check that the batches have the correct memory and cpu
        # values, but Parasol seems to slightly change the values sometimes.
        self.assertNotEqual(batches[0]['ram'], batches[1]['ram'])
        # Need to kill one of the jobs because there are only two cores available
        self.batchSystem.killBatchJobs([job2])
        job3 = self.batchSystem.issueBatchJob(jobDesc1)
        self.assertIsNotNone(job3)
        batches = self._getBatchList()
        self.assertEqual(len(batches), 1)

    def _parseBatchString(self, batchString):
        import re
        batchInfo = dict()
        memPattern = re.compile(r"(\d+\.\d+)([kgmbt])")
        items = batchString.split()
        batchInfo["cores"] = int(items[7])
        memMatch = memPattern.match(items[8])
        ramValue = float(memMatch.group(1))
        ramUnits = memMatch.group(2)
        ramConversion = {'b': 1e0, 'k': 1e3, 'm': 1e6, 'g': 1e9, 't': 1e12}
        batchInfo["ram"] = ramValue * ramConversion[ramUnits]
        return batchInfo

    def _getBatchList(self):
        # noinspection PyUnresolvedReferences
        exitStatus, batchLines = self.batchSystem._runParasol(['list', 'batches'])
        self.assertEqual(exitStatus, 0)
        return [self._parseBatchString(line) for line in batchLines[1:] if line]


@slow
@needs_gridengine
class GridEngineBatchSystemTest(hidden.AbstractGridEngineBatchSystemTest):
    """
    Tests against the GridEngine batch system
    """

    def createBatchSystem(self) -> AbstractBatchSystem:
        from toil.batchSystems.gridengine import GridEngineBatchSystem
        return GridEngineBatchSystem(config=self.config, maxCores=numCores, maxMemory=1000e9,
                                     maxDisk=1e9)

    def tearDown(self):
        super().tearDown()
        # Cleanup GridEngine output log file from qsub
        from glob import glob
        for f in glob('toil_job*.o*'):
            os.unlink(f)


@slow
@needs_slurm
class SlurmBatchSystemTest(hidden.AbstractGridEngineBatchSystemTest):
    """
    Tests against the Slurm batch system
    """

    def createBatchSystem(self) -> AbstractBatchSystem:
        from toil.batchSystems.slurm import SlurmBatchSystem
        return SlurmBatchSystem(config=self.config, maxCores=numCores, maxMemory=1000e9,
                                maxDisk=1e9)

    def tearDown(self):
        super().tearDown()
        # Cleanup 'slurm-%j.out' produced by sbatch
        from glob import glob
        for f in glob('slurm-*.out'):
            os.unlink(f)


@slow
@needs_lsf
class LSFBatchSystemTest(hidden.AbstractGridEngineBatchSystemTest):
    """
    Tests against the LSF batch system
    """
    def createBatchSystem(self) -> AbstractBatchSystem:
        from toil.batchSystems.lsf import LSFBatchSystem
        return LSFBatchSystem(config=self.config, maxCores=numCores,
                              maxMemory=1000e9, maxDisk=1e9)


@slow
@needs_torque
class TorqueBatchSystemTest(hidden.AbstractGridEngineBatchSystemTest):
    """
    Tests against the Torque batch system
    """

    def _createDummyConfig(self):
        config = super()._createDummyConfig()
        # can't use _getTestJobStorePath since that method removes the directory
        config.jobStore = self._createTempDir('jobStore')
        return config

    def createBatchSystem(self) -> AbstractBatchSystem:
        from toil.batchSystems.torque import TorqueBatchSystem
        return TorqueBatchSystem(config=self.config, maxCores=numCores, maxMemory=1000e9,
                                     maxDisk=1e9)

    def tearDown(self):
        super().tearDown()
        # Cleanup 'toil_job-%j.out' produced by sbatch
        from glob import glob
        for f in glob('toil_job_*.[oe]*'):
            os.unlink(f)


@slow
@needs_htcondor
class HTCondorBatchSystemTest(hidden.AbstractGridEngineBatchSystemTest):
    """
    Tests against the HTCondor batch system
    """

    def createBatchSystem(self) -> AbstractBatchSystem:
        from toil.batchSystems.htcondor import HTCondorBatchSystem
        return HTCondorBatchSystem(config=self.config, maxCores=numCores, maxMemory=1000e9,
                                   maxDisk=1e9)

    def tearDown(self):
        super().tearDown()


class SingleMachineBatchSystemJobTest(hidden.AbstractBatchSystemJobTest):
    """
    Tests Toil workflow against the SingleMachine batch system
    """

    def getBatchSystemName(self):
        return "single_machine"

    @slow
    @retry_flaky_test(prepare=[hidden.AbstractBatchSystemJobTest.tearDown, hidden.AbstractBatchSystemJobTest.setUp])
    def testConcurrencyWithDisk(self):
        """
        Tests that the batch system is allocating disk resources properly
        """
        tempDir = self._createTempDir('testFiles')

        options = Job.Runner.getDefaultOptions(self._getTestJobStorePath())
        options.workDir = tempDir
        from toil import physicalDisk
        availableDisk = physicalDisk(options.workDir)
        logger.info('Testing disk concurrency limits with %s disk space', availableDisk)
        # More disk might become available by the time Toil starts, so we limit it here
        options.maxDisk = availableDisk
        options.batchSystem = self.batchSystemName

        counterPath = os.path.join(tempDir, 'counter')
        resetCounters(counterPath)
        value, maxValue = getCounters(counterPath)
        assert (value, maxValue) == (0, 0)

        half_disk = availableDisk // 2
        more_than_half_disk = half_disk + 500
        logger.info('Dividing into parts of %s and %s', half_disk, more_than_half_disk)

        root = Job()
        # Physically, we're asking for 50% of disk and 50% of disk + 500bytes in the two jobs. The
        # batchsystem should not allow the 2 child jobs to run concurrently.
        root.addChild(Job.wrapFn(measureConcurrency, counterPath, self.sleepTime, cores=1,
                                 memory='1M', disk=half_disk))
        root.addChild(Job.wrapFn(measureConcurrency, counterPath, self.sleepTime, cores=1,
                                 memory='1M', disk=more_than_half_disk))
        Job.Runner.startToil(root, options)
        _, maxValue = getCounters(counterPath)

        logger.info('After run: %s disk space', physicalDisk(options.workDir))

        self.assertEqual(maxValue, 1)

    @skipIf(SingleMachineBatchSystem.numCores < 4, 'Need at least four cores to run this test')
    @slow
    def testNestedResourcesDoNotBlock(self):
        """
        Resources are requested in the order Memory > Cpu > Disk.
        Test that unavailability of cpus for one job that is scheduled does not block another job
        that can run.
        """
        tempDir = self._createTempDir('testFiles')

        options = Job.Runner.getDefaultOptions(self._getTestJobStorePath())
        options.workDir = tempDir
        options.maxCores = 4
        from toil import physicalMemory
        availableMemory = physicalMemory()
        options.batchSystem = self.batchSystemName

        outFile = os.path.join(tempDir, 'counter')
        open(outFile, 'w').close()

        root = Job()

        blocker = Job.wrapFn(_resourceBlockTestAuxFn, outFile=outFile, sleepTime=30, writeVal='b',
                             cores=2, memory='1M', disk='1M')
        firstJob = Job.wrapFn(_resourceBlockTestAuxFn, outFile=outFile, sleepTime=5, writeVal='fJ',
                              cores=1, memory='1M', disk='1M')
        secondJob = Job.wrapFn(_resourceBlockTestAuxFn, outFile=outFile, sleepTime=10,
                               writeVal='sJ', cores=1, memory='1M', disk='1M')

        # Should block off 50% of memory while waiting for it's 3 cores
        firstJobChild = Job.wrapFn(_resourceBlockTestAuxFn, outFile=outFile, sleepTime=0,
                                   writeVal='fJC', cores=3, memory=int(availableMemory // 2), disk='1M')

        # These two shouldn't be able to run before B because there should be only
        # (50% of memory - 1M) available (firstJobChild should be blocking 50%)
        secondJobChild = Job.wrapFn(_resourceBlockTestAuxFn, outFile=outFile, sleepTime=5,
                                    writeVal='sJC', cores=2, memory=int(availableMemory // 1.5),
                                    disk='1M')
        secondJobGrandChild = Job.wrapFn(_resourceBlockTestAuxFn, outFile=outFile, sleepTime=5,
                                         writeVal='sJGC', cores=2, memory=int(availableMemory // 1.5),
                                         disk='1M')

        root.addChild(blocker)
        root.addChild(firstJob)
        root.addChild(secondJob)

        firstJob.addChild(firstJobChild)
        secondJob.addChild(secondJobChild)

        secondJobChild.addChild(secondJobGrandChild)
        """
        The tree is:
                    root
                  /   |   \
                 b    fJ   sJ
                      |    |
                      fJC  sJC
                           |
                           sJGC
        But the order of execution should be
        root > b , fJ, sJ > sJC > sJGC > fJC
        since fJC cannot run till bl finishes but sJC and sJGC can(fJC blocked by disk). If the
        resource acquisition is written properly, then fJC which is scheduled before sJC and sJGC
        should not block them, and should only run after they finish.
        """
        Job.Runner.startToil(root, options)
        with open(outFile) as oFH:
            outString = oFH.read()
        # The ordering of b, fJ and sJ is non-deterministic since they are scheduled at the same
        # time. We look for all possible permutations.
        possibleStarts = tuple([''.join(x) for x in itertools.permutations(['b', 'fJ', 'sJ'])])
        assert outString.startswith(possibleStarts)
        assert outString.endswith('sJCsJGCfJC')


def _resourceBlockTestAuxFn(outFile, sleepTime, writeVal):
    """
    Write a value to the out file and then sleep for requested seconds.
    :param str outFile: File to write to
    :param int sleepTime: Time to sleep for
    :param str writeVal: Character to write
    """
    with open(outFile, 'a') as oFH:
        fcntl.flock(oFH, fcntl.LOCK_EX)
        oFH.write(writeVal)
    time.sleep(sleepTime)


@slow
@needs_mesos
class MesosBatchSystemJobTest(hidden.AbstractBatchSystemJobTest, MesosTestSupport):
    """
    Tests Toil workflow against the Mesos batch system
    """
    def getOptions(self, tempDir):
        options = super().getOptions(tempDir)
        options.mesos_endpoint = 'localhost:5050'
        return options

    def getBatchSystemName(self):
        self._startMesos(self.cpuCount)
        return "mesos"

    def tearDown(self):
        self._stopMesos()


def measureConcurrency(filepath, sleep_time=10):
    """
    Run in parallel to determine the number of concurrent tasks.
    This code was copied from toil.batchSystemTestMaxCoresSingleMachineBatchSystemTest
    :param str filepath: path to counter file
    :param int sleep_time: number of seconds to sleep before counting down
    :return int max concurrency value:
    """
    count(1, filepath)
    try:
        time.sleep(sleep_time)
    finally:
        return count(-1, filepath)


def count(delta, file_path):
    """
    Increments counter file and returns the max number of times the file
    has been modified. Counter data must be in the form:
    concurrent tasks, max concurrent tasks (counter should be initialized to 0,0)

    :param int delta: increment value
    :param str file_path: path to shared counter file
    :return int max concurrent tasks:
    """
    fd = os.open(file_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            s = os.read(fd, 10)
            value, maxValue = [int(i) for i in s.decode('utf-8').split(',')]
            value += delta
            if value > maxValue: maxValue = value
            os.lseek(fd, 0, 0)
            os.ftruncate(fd, 0)
            os.write(fd, f'{value},{maxValue}'.encode('utf-8'))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    return maxValue


def getCounters(path):
    with open(path, 'r+') as f:
        concurrentTasks, maxConcurrentTasks = [int(i) for i in f.read().split(',')]
    return concurrentTasks, maxConcurrentTasks


def resetCounters(path):
    with open(path, "w") as f:
        f.write("0,0")
        f.close()


def get_omp_threads() -> str:
    return os.environ['OMP_NUM_THREADS']
