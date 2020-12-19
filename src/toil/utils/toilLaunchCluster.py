# Copyright (C) 2015-2020 UCSC Computational Genomics Lab
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
Launches a toil leader instance with the specified provisioner.
"""
import logging
from toil.lib.bioio import parseBasicOptions, getBasicOptionParser
from toil.utils import addBasicProvisionerOptions, getZoneFromEnv
from toil.provisioners import clusterFactory
from toil.provisioners.aws import checkValidNodeTypes
from toil import applianceSelf

logger = logging.getLogger(__name__)


def createTagsDict(tagList):
    tagsDict = dict()
    for tag in tagList:
        key, value = tag.split('=')
        tagsDict[key] = value
    return tagsDict


def main():
    parser = getBasicOptionParser()
    parser = addBasicProvisionerOptions(parser)
    parser.add_argument("-T", "--clusterType", dest="clusterType",
                        choices=['mesos', 'kubernetes'], default='mesos',
                        help="Cluster scheduler to use.")
    parser.add_argument("--leaderNodeType", dest="leaderNodeType", required=True,
                        help="Non-preemptable node type to use for the cluster leader.")
    parser.add_argument("--keyPairName", dest='keyPairName',
                        help="On AWS, the name of the AWS key pair to include on the instance."
                        " On Google/GCE, this is the ssh key pair.")
    parser.add_argument("--owner", dest='owner',
                        help="The owner tag for all instances. If not given, the value in"
                        " --keyPairName will be used if given.")
    parser.add_argument("--boto", dest='botoPath',
                        help="The path to the boto credentials directory. This is transferred "
                        "to all nodes in order to access the AWS jobStore from non-AWS instances.")
    parser.add_argument("-t", "--tag", metavar='NAME=VALUE', dest='tags',
                        default=[], action='append',
                        help="Tags are added to the AWS cluster for this node and all of its "
                             "children. Tags are of the form:\n"
                             " -t key1=value1 --tag key2=value2\n"
                             "Multiple tags are allowed and each tag needs its own flag. By "
                             "default the cluster is tagged with "
                             " {\n"
                             "      \"Name\": clusterName,\n"
                             "      \"Owner\": IAM username\n"
                             " }. ")
    parser.add_argument("--vpcSubnet",
                        help="VPC subnet ID to launch cluster in. Uses default subnet if not "
                        "specified. This subnet needs to have auto assign IPs turned on.")
    parser.add_argument("--nodeTypes", dest='nodeTypes', default=None, type=str,
                        help="Comma-separated list of node types to create while launching the "
                             "leader. The syntax for each node type depends on the provisioner "
                             "used. For the aws provisioner this is the name of an EC2 instance "
                             "type followed by a colon and the price in dollar to bid for a spot "
                             "instance, for example 'c3.8xlarge:0.42'. Must also provide the "
                             "--workers or --managedWorkers arguments to specify how many workers of "
                             "each node type to create.")
    parser.add_argument("-w", "--workers", dest='workers', default=None, type=str,
                        help="Comma-separated list of the number of workers of each node type to "
                             "launch alongside the leader when the cluster is created. This can be "
                             "useful if running toil without auto-scaling but with need of more "
                             "hardware support")
    parser.add_argument("-W", "--managedWorkers", dest='managedWorkers', default=None, type=str,
                        help="Comma-separated list of one number per node type in --nodeTypes. "
                             "The cluster will automatically deploy workers of that type, when "
                             "needed, up to the given limit.")
    parser.add_argument("--leaderStorage", dest='leaderStorage', type=int, default=50,
                        help="Specify the size (in gigabytes) of the root volume for the leader "
                             "instance.  This is an EBS volume.")
    parser.add_argument("--nodeStorage", dest='nodeStorage', type=int, default=50,
                        help="Specify the size (in gigabytes) of the root volume for any worker "
                             "instances created when using the -w flag. This is an EBS volume.")
    parser.add_argument('--forceDockerAppliance', dest='forceDockerAppliance', action='store_true',
                        default=False,
                        help="Disables sanity checking the existence of the docker image specified "
                             "by TOIL_APPLIANCE_SELF, which Toil uses to provision mesos for "
                             "autoscaling.")
    parser.add_argument('--awsEc2ProfileArn', dest='awsEc2ProfileArn', default=None, type=str,
                        help="If provided, the specified ARN is used as the instance profile for EC2 instances."
                             "Useful for setting custom IAM profiles. If not specified, a new IAM role is created "
                             "by default with sufficient access to perform basic cluster operations.")
    parser.add_argument('--awsEc2ExtraSecurityGroupId', dest='awsEc2ExtraSecurityGroupIds', default=[], action='append',
                        help="Any additional security groups to attach to EC2 instances. Note that a security group "
                             "with its name equal to the cluster name will always be created, thus ensure that "
                             "the extra security groups do not have the same name as the cluster name.")
    config = parseBasicOptions(parser)
    tags = createTagsDict(config.tags) if config.tags else dict()
    checkValidNodeTypes(config.provisioner, config.nodeTypes)
    checkValidNodeTypes(config.provisioner, config.leaderNodeType)


    # checks the validity of TOIL_APPLIANCE_SELF before proceeding
    applianceSelf(forceDockerAppliance=config.forceDockerAppliance)
   
    # This holds (instance type name, bid or None) tuples for each node type.
    # No bid means non-preemptable
    parsedNodeTypes = []
    # This holds how many of each kind of node to make (or bid on) explicitly at cluster startup
    fixedNodeCounts = []
    # This holds how many to limit autoscaling to when using managed nodes scaled by the cluster
    managedNodeCounts = [] 
   
    if (config.nodeTypes or (config.managedWorkers or config.workers)) and not (config.nodeTypes and (config.managedWorkers or config.workers)):
        raise RuntimeError("The --nodeTypes option requires one of --workers or --managedWorkers, and visa versa.")
    if config.nodeTypes:
        for nodeTypeStr in config.nodeTypes.split(","):
            parsedBid = nodeTypeStr.split(':', 1)
            if len(nodeTypeStr) != len(parsedBid[0]):
                #Is a preemptable node
                parsedNodeTypes.append((parsedBid[0], float(parsedBid[1])))
            else:
                # Is a normal node
                parsedNodeTypes.append((nodeTypeStr, None))
                
        if config.workers:
            numWorkersList = config.workers.split(",")
            if not len(parsedNodeTypes) == len(numWorkersList):
                raise RuntimeError("List of worker counts must be the same length as the list of node types.")
            fixedNodeCounts = [int(x) for x in numWorkersList]
        if config.managedWorkers:
            managedWorkersList = config.workers.split(",")
            if not len(parsedNodeTypes) == len(managedWorkersList):
                raise RuntimeError("List of max worker counts must be the same length as the list of node types.")
            managedNodeCounts = [int(x) for x in managedWorkersList]

    owner = config.owner or config.keyPairName or 'toil'

    # Check to see if the user specified a zone. If not, see if one is stored in an environment variable.
    config.zone = config.zone or getZoneFromEnv(config.provisioner)

    if not config.zone:
        raise RuntimeError('Please provide a value for --zone or set a default in the TOIL_' +
                           config.provisioner.upper() + '_ZONE environment variable.')

    cluster = clusterFactory(provisioner=config.provisioner,
                             clusterName=config.clusterName,
                             clusterType=config.clusterType,
                             zone=config.zone,
                             nodeStorage=config.nodeStorage)

    cluster.launchCluster(leaderNodeType=config.leaderNodeType,
                          leaderStorage=config.leaderStorage,
                          owner=owner,
                          keyName=config.keyPairName,
                          botoPath=config.botoPath,
                          userTags=tags,
                          vpcSubnet=config.vpcSubnet,
                          awsEc2ProfileArn=config.awsEc2ProfileArn,
                          awsEc2ExtraSecurityGroupIds=config.awsEc2ExtraSecurityGroupIds)

    for typeNum, count in enumerate(fixedNodeCounts):
        # For each batch of workers to make at startup
        if count == 0:
            # Don't want any
            continue
        wanted = parsedNodeTypes[typeNum]
        if wanted[1] is None:
            # Make non-spot instances
            cluster.addNodes(nodeType=wanted[0], numNodes=count, preemptable=False)
        else:
            # We have a spot bid
            cluster.addNodes(nodeType=wanted[0], numNodes=count, preemptable=True,
                             spotBid=wanted[1])
                             
    for typeNum, count in enumerate(managedNodeCounts):
        # For each batch of workers to dynamically scale
        if count == 0:
            # Don't want any
            continue
        wanted = parsedNodeTypes[typeNum]
        if wanted[1] is None:
            # Make non-spot instances
            cluster.addManagedNodes(nodeType=wanted[0], numNodes=count, preemptable=False)
        else:
            # Bid at the given price.
            cluster.addManagedNodes(nodeType=wanted[0], numNodes=count, preemptable=True,
                                    spotBid=wanted[1])
            
        

