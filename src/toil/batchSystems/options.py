# Copyright (C) 2015-2016 Regents of the University of California
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
#

from registry import batchSystemFactoryFor, defaultBatchSystem, uniqueNames


def __parasolOptions(addOptionFn):
    addOptionFn("--parasolCommand", dest="parasolCommand", default=None,
                      help="The name or path of the parasol program. Will be looked up on PATH "
                           "unless it starts with a slashdefault=%s" % 'parasol')
    addOptionFn("--parasolMaxBatches", dest="parasolMaxBatches", default=None,
                help="Maximum number of job batches the Parasol batch is allowed to create. One "
                     "batch is created for jobs with a a unique set of resource requirements. "
                     "default=%i" % 1000)
    
def __singleMachineOptions(addOptionFn):
    addOptionFn("--scale", dest="scale", default=None,
        help=("A scaling factor to change the value of all submitted tasks's submitted cores. "
              "Used in singleMachine batch system. default=%s" % 1))

def __mesosOptions(addOptionFn):
    addOptionFn("--mesosMaster", dest="mesosMasterAddress", default=None,
        help=("The host and port of the Mesos master separated by colon. default=%s" % 'localhost:5050'))

__OPTIONS = [
    __parasolOptions,
    __singleMachineOptions,
    __mesosOptions
    ]

__options = list(__OPTIONS)

def addOptionsDefinition(optionsDefinition):
    __options.append(optionsDefinition)
    
    
def setOptions(config, setOption):
    batchSystem = config.batchSystem
    
    factory = batchSystemFactoryFor(batchSystem)
    batchSystem = factory()
    
    batchSystem.setOptions(setOption)
    
def addOptions(addOptionFn):
        
    addOptionFn("--batchSystem", dest="batchSystem", default=defaultBatchSystem(),
              help=("The type of batch system to run the job(s) with, currently can be one "
                    "of %s'. default=%s" % (', '.join(uniqueNames()), defaultBatchSystem())))
    
    for o in __options:
        o(addOptionFn)
