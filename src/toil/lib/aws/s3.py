# Copyright (C) 2015-2024 Regents of the University of California
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
import logging
from typing import Dict, Any, Optional
from toil.lib.aws import session, AWSServerErrors
from toil.lib.retry import retry

logger = logging.getLogger(__name__)


@retry(errors=[AWSServerErrors])
def head_s3_object(bucket: str, key: str, header: Dict[str, Any], region: Optional[str] = None):
    s3_client = session.client("s3", region_name=region)
    return s3_client.head_object(Bucket=bucket, Key=key, **header)
