# Toil Environment Variables
#
# Configure how toil runs in different environments
#
# Source this file in your bash shell using "source environment.sh".

SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ] ; do SOURCE="$(readlink "$SOURCE")"; done

set -a

# the directory this file is in
TOIL_HOME="$(cd -P "$(dirname "$SOURCE")" && pwd)"

############
### MAIN ###
############

# specifies the docker image used for all toil autoscaling runs
TOIL_DOCKER_REGISTRY=quay.io/ucsc_cgl
TOIL_DOCKER_NAME=toil
TOIL_DOCKER_TAG=$(python $TOIL_HOME/version_template.py dockerTag)
TOIL_APPLIANCE_SELF=$TOIL_DOCKER_REGISTRY/$TOIL_DOCKER_NAME:$TOIL_DOCKER_TAG

# An absolute path to a directory where Toil will write its temporary files. This directory
# must exist on each worker node and may be set to a different value on each worker. The
# ``--workDir`` command line option overrides this. On Mesos nodes, ``TOIL_WORKDIR`` generally
# defaults to the Mesos sandbox, except on CGCloud-provisioned nodes where it defaults to
# ``/var/lib/mesos``. In all other cases, the system's standard temporary directory is used.
TOIL_WORKDIR=''  # unset unless filled in

# Determines if Toil will try to refer back to a Python virtual environment in
# which it is installed when composing commands that may be run on other hosts. If set to ``True``, if
# Toil is installed in the current virtual environment, it will use absolute paths to its own
# executables (and the virtual environment must thus be available on at the same path on all nodes).
# Otherwise, Toil internal commands such as ``_toil_worker`` will be resolved according to the
# ``PATH`` on the node where they are executed. This setting can be useful in a shared HPC environment,
# where users may have their own Toil installations in virtual environments.
TOIL_CHECK_ENV=''  # unset unless filled in

###########
### AWS ###
###########

# Specifies the AWS region/zone to run in.
TOIL_AWS_ZONE=us-west-2a

# If set to ``True``, nodes that fail EC2 health checks won't be terminated so they can be examined and the cause
# of failure determined.  Also leaves in place associated IAM and security groups.
TOIL_AWS_NODE_DEBUG=False

# ID of the (normally CoreOS) AMI to use in node provisioning.
# If in doubt, don't set this variable (defaults to latest CoreOS).
TOIL_AWS_AMI=''  # unset unless filled in

#######################
### KUBERNETES ONLY ###
#######################

# Get the name of the AWS secret, if any, to mount in containers.
TOIL_AWS_SECRET_NAME=''  # unset unless filled in

##############
### GOOGLE ###
##############

# Project ID required to to run the google cloud tests.
TOIL_GOOGLE_PROJECTID=''  # unset unless filled in

# GOOGLE_APPLICATION_CREDENTIALS is also checked, but is used for other programs so we don't set it here

###########
### HPC ###
###########

TOIL_SLURM_ARGS=''  # unset unless filled in
TOIL_GRIDENGINE_ARGS=''  # unset unless filled in
TOIL_GRIDENGINE_PE=''  # unset unless filled in
TOIL_TORQUE_ARGS=''  # unset unless filled in
TOIL_TORQUE_REQS=''  # unset unless filled in
TOIL_LSF_ARGS=''  # unset unless filled in
TOIL_HTCONDOR_PARAMS=''  # unset unless filled in

############
### MISC ###
############

# Any custom bash command to run in the Toil docker container prior to running the Toil services.  Can be used
# for any custom initialization in the worker and/or primary nodes such as private docker docker authentication.
# Example for AWS ECR:
#     ``pip install awscli && eval $(aws ecr get-login --no-include-email --region us-east-1)``
TOIL_CUSTOM_DOCKER_INIT_COMMAND=''  # unset unless filled in

set +a
