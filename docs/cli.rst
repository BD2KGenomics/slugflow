Command Line Interface and Arguments
====================================

Toil provides many command line options when running a toil script (see :ref:`running`), 
or using Toil to run a CWL or WDL script. Many of these are described below. 
For most Toil scripts executing '--help' will show this list of options.

It is also possible to set and manipulate the options described when invoking a 
Toil workflow from within Python using :func:`toil.job.Job.Runner.getDefaultOptions`, e.g.::

    options = Job.Runner.getDefaultOptions("./toilWorkflow") # Get the options object
    options.logLevel = "INFO" # Set the log level to the info level.
    
    Job.Runner.startToil(Job(), options) # Run the script
 

Logging
-------
Toil hides stdout and stderr by default except in case of job failure.
For more robust logging options (default is INFO), use ``--logDebug`` or more generally, use
``--logLevel=``, which may be set to either ``OFF`` (or ``CRITICAL``), ``ERROR``, ``WARN`` (or ``WARNING``),
``INFO`` or ``DEBUG``. Logs can be directed to a file with ``--logFile=``.

If large logfiles are a problem, ``--maxLogFileSize`` (in bytes) can be set as well as ``--rotatingLogging``, which
prevents logfiles from getting too large.

Stats
-----
The ``--stats`` argument records statistics about the Toil workflow in the job store. After a Toil run has finished,
the entrypoint ``toil stats <jobStore>`` can be used to return statistics about cpu, memory, job duration, and more.
The job store will never be deleted with ``--stats``, as it overrides ``--clean``.

Restart
-------
In the event of failure, Toil can resume the pipeline by adding the argument ``--restart`` and rerunning the
python script. The entrypoint ``toil restart <jobStore>``, can also be used. Toil pipelines can even be
edited and resumed which is useful for development or troubleshooting.

Clean
-----
If a Toil pipeline didn't finish successfully, or is using a variation of ``--clean``, the job store will exist
until it is deleted. ``toil clean <jobStore>`` ensures that all artifacts associated with a job store are removed.
This is particularly useful for deleting AWS job stores, which reserves an SDB domain as well as an S3 bucket.

The deletion of the job store can be modified by the ``--clean`` argument, and may be set to ``always``, ``onError``,
``never``, or ``onSuccess`` (default).

Temporary directories where jobs are running can also be saved from deletion using the ``--cleanWorkDir``, which has
the same options as ``--clean``.  This option should only be run when debugging, as intermediate jobs will fill up
disk space.


BatchSystem
-----------

Toil supports several different batch systems using the ``--batchSystem`` argument.
More information in the :ref:`batchsysteminterface`.


Default Cores, Disk, and Memory
-------------------------------

Toil uses resource requirements to intelligently schedule jobs. The defaults for cores (1), disk (2G), and memory (2G),
can all be changed using ``--defaultCores``, ``--defaultDisk``, and ``--defaultMemory``. Standard suffixes
like K, Ki, M, Mi, G or Gi are supported.


Miscellaneous
-------------
Here are some additional useful arguments that don't fit into another category.

* ``--workDir`` sets the location where temporary directories are created for running jobs.
* ``--retryCount`` sets the number of times to retry a job in case of failure. Useful for non-systemic failures like HTTP requests.
* ``--sseKey`` accepts a path to a 32-byte key that is used for server-side encryption when using the AWS job store.
* ``--cseKey`` accepts a path to a 256-bit key to be used for client-side encryption on Azure job store.
* ``--setEnv <NAME=VALUE>`` sets an environment variable early on in the worker