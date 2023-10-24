import os
from toil.lib.io import mkdtemp

from toil.common import Toil
from toil.job import Job

if __name__ == "__main__":
    # A
    A = Job()
    A.addChild(Job())
    A.addFollowOn(Job())

    # Encapsulate A
    A = A.encapsulate()

    # B is a job which needs to run after A and its successors
    B = Job()

    # With encapsulation A and its successor subgraph appear to be a single job, hence:
    A.addChild(B)

    jobstore: str = mkdtemp("tutorial_encapsulations2")
    os.rmdir(jobstore)
    options = Job.Runner.getDefaultOptions(jobstore)
    options.logLevel = "INFO"
    options.clean = "always"

    with Toil(options) as toil:
        print(toil.start(A))
