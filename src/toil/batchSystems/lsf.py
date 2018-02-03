# Copyright (C) 2013 by Thomas Keane (tk2@sanger.ac.uk)
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
from __future__ import absolute_import
from __future__ import division
from builtins import str
from builtins import range
from past.utils import old_div
import logging
import subprocess
import time
from datetime import date
import os

from toil.batchSystems import MemoryString
from toil.batchSystems.abstractGridEngineBatchSystem import \
        AbstractGridEngineBatchSystem
from toil.batchSystems.lsfHelper import (parse_memory_resource,
                                         parse_memory_limit,
                                         per_core_reservation)

logger = logging.getLogger(__name__)


class LSFBatchSystem(AbstractGridEngineBatchSystem):

    class Worker(AbstractGridEngineBatchSystem.Worker):
        """LSF specific AbstractGridEngineWorker methods."""

        def getRunningJobIDs(self):
            times = {}
            currentjobs = dict((str(self.batchJobIDs[x][0]), x) for x in
                               self.runningJobs)
            process = subprocess.Popen(["bjobs", "-o",
                                        "jobid stat start_time delimiter="
                                        + chr(30)], stdout=subprocess.PIPE)
            stdout, stderr = process.communicate()

            for curline in process.stdout:
                items = curline.strip().split(chr(30))
                if items[0] in currentjobs and items[1] == 'RUN':
                    jobstart = time.strptime("%s %s %s" % items[2].split(),
                                             str(date.today().year),
                                             "%b %d %H:%M")
                    times[currentjobs[items[0]]] = time.time() - jobstart
            return times

        def killJob(self, jobID):
            subprocess.check_call(['bkill', self.getBatchSystemID(jobID)])

        def prepareSubmission(self, cpu, memory, jobID, command):
            return self.prepareBsub(cpu, memory, jobID) + [command]

        def submitJob(self, subLine):
            process = subprocess.Popen(subLine, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT,
                                       env=self.boss.environment)
            line = process.stdout.readline()
            logger.debug("BSUB: " + line)
            result = int(line.strip().split()[1].strip('<>'))
            logger.debug("Got the job id: %s" % (str(result)))
            return result

        def getJobExitCode(self, lsfJobID):
            # the task is set as part of the job ID if using getBatchSystemID()
            job, task = (lsfJobID, None)
            if '.' in lsfJobID:
                job, task = lsfJobID.split('.', 1)

            # first try bjobs to find out job state
            args = ["bjobs", "-l", str(job)]
            logger.debug("Checking job exit code for job via bjobs: %d" % job)
            process = subprocess.Popen(args, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT)
            started = 0
            for line in process.stdout:
                if line.find("Done successfully") > -1:
                    logger.debug("bjobs detected job completed for job: %d"
                                 % job)
                    return 0
                elif line.find("Completed <exit>") > -1:
                    logger.debug("bjobs detected job failed for job: %d"
                                 % job)
                    return 1
                elif line.find("New job is waiting for scheduling") > -1:
                    logger.debug("bjobs detected job pending scheduling for "
                                 "job: %d" % job)
                    return None
                elif line.find("PENDING REASONS") > -1:
                    logger.debug("bjobs detected job pending for job: %d"
                                 % job)
                    return None
                elif line.find("Started on ") > -1:
                    started = 1

            if started == 1:
                logger.debug("bjobs detected job started but not completed: %d"
                             % job)
                return None

            # if not found in bjobs, then try bacct (slower than bjobs)
            logger.debug("bjobs failed to detect job - trying bacct: %d" % job)

            args = ["bacct", "-l", str(job)]
            process = subprocess.Popen(args, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT)
            for line in process.stdout:
                if line.find("Completed <done>") > -1:
                    logger.debug("Detected job completed for job: %d" % job)
                    return 0
                elif line.find("Completed <exit>") > -1:
                    logger.debug("Detected job failed for job: %d" % job)
                    return 1
            logger.debug("Can't determine exit code for job or job still "
                         "running: %d" % job)
            return None

        """
        Implementation-specific helper methods
        """
        @staticmethod
        def prepareBsub(cpu, mem, jobID):
            """
            Make a bsub commandline to execute.

            params:
              cpu: number of cores needed
              mem: number of bytes of memory needed
              jobID: ID number of the job
            """
            if mem:
                if per_core_reservation():
                    mem = float(mem)/1024**3/int(cpu)
                    mem_resource = parse_memory_resource(mem)
                    mem_limit = parse_memory_limit(mem)
                else:
                    mem = old_div(float(mem), 1024**3)
                    mem_resource = parse_memory_resource(mem)
                    mem_limit = parse_memory_limit(mem)
                bsubMem = ['-R', 'select[type==X86_64 && mem > %d] '
                           'rusage[mem=%d]' % mem_resource, mem_resource,
                           '-M', str(mem_limit)]
            else:
                bsubMem = []
            bsubCpu = [] if cpu is None else ['-n', str(int(cpu))]
            bsubline = ["bsub", "-cwd", ".", "-o", "/dev/null",
                        "-e", "/dev/null", "-J", "toil_job_%d" % jobID]
            bsubline.extend(bsubMem)
            bsubline.extend(bsubCpu)
            lsfArgs = os.getenv('TOIL_LSF_ARGS')
            if lsfArgs:
                bsubline.extend(lsfArgs.split())
            return bsubline

    def getWaitDuration(self):
        """We give LSF a second to catch its breath (in seconds)
        """
        return 15

    @classmethod
    def obtainSystemConstants(cls):
        p = subprocess.Popen(["lshosts"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)

        line = p.stdout.readline()
        items = line.strip().split()
        num_columns = len(items)
        cpu_index = None
        mem_index = None
        for i in range(num_columns):
                if items[i] == 'ncpus':
                        cpu_index = i
                elif items[i] == 'maxmem':
                        mem_index = i

        if cpu_index is None or mem_index is None:
                RuntimeError("lshosts command does not return ncpus or maxmem "
                             "columns")

        # p.stdout.readline()

        maxCPU = 0
        maxMEM = MemoryString("0")
        for line in p.stdout:
                items = line.strip().split()
                if len(items) < num_columns:
                        RuntimeError("lshosts output has a varying number of "
                                     "columns")
                if items[cpu_index] != '-' and items[cpu_index] > maxCPU:
                        maxCPU = items[cpu_index]
                if (items[mem_index] != '-' and
                        MemoryString(items[mem_index]) > maxMEM):
                    maxMEM = MemoryString(items[mem_index])

        if maxCPU is 0 or maxMEM is 0:
                RuntimeError("lshosts returns null ncpus or maxmem info")
        logger.debug("Got the maxCPU: %s" % (maxMEM))

        return maxCPU, maxMEM
