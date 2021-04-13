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
import logging

from toil.lib.conversions import convert_units, MemoryString, human2bytes
from toil.test import ToilTest

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)


class ConversionTest(ToilTest):
    def test_convert(self):
        expected_conversions = {
            "0 B": "0.0000 TB",
            "0 KB": "0.0000 TB",
            "0 MB": "0.0000 TB",
            "0 GB": "0.0000 TB",
            "0 TB": "0.0000 TB",
            "0.1 B": "0.0000 TB",
            "0.1 KB": "0.0000 TB",
            "0.1 MB": "0.0000 TB",
            "0.1 GB": "0.0001 TB",
            "0.1 TB": "0.1000 TB",
            "0.5 B": "0.0000 TB",
            "0.5 KB": "0.0000 TB",
            "0.5 MB": "0.0000 TB",
            "0.5 GB": "0.0005 TB",
            "0.5 TB": "0.5000 TB",
            "0.9 B": "0.0000 TB",
            "0.9 KB": "0.0000 TB",
            "0.9 MB": "0.0000 TB",
            "0.9 GB": "0.0009 TB",
            "0.9 TB": "0.9000 TB",
            "1 B": "0.0000 TB",
            "1 KB": "0.0000 TB",
            "1 MB": "0.0000 TB",
            "1 GB": "0.0010 TB",
            "1 TB": "1.0000 TB",
            "7 B": "0.0000 TB",
            "7 KB": "0.0000 TB",
            "7 MB": "0.0000 TB",
            "7 GB": "0.0070 TB",
            "7 TB": "7.0000 TB",
            "7.42423 B": "0.0000 TB",
            "7.42423 KB": "0.0000 TB",
            "7.42423 MB": "0.0000 TB",
            "7.42423 GB": "0.0074 TB",
            "7.42423 TB": "7.4242 TB",
            "10 B": "0.0000 TB",
            "10 KB": "0.0000 TB",
            "10 MB": "0.0000 TB",
            "10 GB": "0.0100 TB",
            "10 TB": "10.0000 TB",
            "100 B": "0.0000 TB",
            "100 KB": "0.0000 TB",
            "100 MB": "0.0001 TB",
            "100 GB": "0.1000 TB",
            "100 TB": "100.0000 TB",
            "1000 B": "0.0000 TB",
            "1000 KB": "0.0000 TB",
            "1000 MB": "0.0010 TB",
            "1000 GB": "1.0000 TB",
            "1000 TB": "1000.0000 TB",
            "11234234 B": "0.0000 TB",
            "11234234 KB": "0.0112 TB",
            "11234234 MB": "11.2342 TB",
            "11234234 GB": "11234.2340 TB",
            "11234234 TB": "11234234.0000 TB"
        }
        results = {}
        for i in (0, 0.1, 0.5, 0.9, 1, 7, 7.42423, 10, 100, 1000, 11234234):
            for src_unit in ['B', 'KB', 'MB', 'GB', 'TB']:
                for dst_unit in ['B', 'KB', 'MB', 'GB', 'TB']:
                    converted = convert_units(i, src_unit, dst_unit)
                    results[f'{i} {src_unit}'] = f'{converted:.4f} {dst_unit}'
        self.assertEqual(results, expected_conversions)

    def test_memory_string(self):
        self.assertEqual(MemoryString('0'), MemoryString('0M'))
        self.assertEqual(MemoryString('1024'), MemoryString('1K'))
        self.assertEqual(MemoryString('1024.000M'), MemoryString('1G'))
        self.assertEqual(MemoryString('1024G'), MemoryString('1T'))

        self.assertEqual(MemoryString('0').bytes, 0)
        self.assertEqual(MemoryString('1K').bytes, 1024)
        self.assertEqual(MemoryString('1MB').bytes, 1048576)
        self.assertEqual(MemoryString('1024MB').bytes, 1073741824)
        self.assertEqual(MemoryString('1GB').bytes, 1073741824)

    def test_human2bytes(self):
        expected_results = {
            '0 Ki': 0,
            '0 Mi': 0,
            '0 Gi': 0,
            '0 Ti': 0,
            '0.1 Ki': 102,
            '0.1 Mi': 104857,
            '0.1 Gi': 107374182,
            '0.1 Ti': 109951162777,
            '0.5 Ki': 512,
            '0.5 Mi': 524288,
            '0.5 Gi': 536870912,
            '0.5 Ti': 549755813888,
            '0.9 Ki': 921,
            '0.9 Mi': 943718,
            '0.9 Gi': 966367641,
            '0.9 Ti': 989560464998,
            '1 Ki': 1024,
            '1 Mi': 1048576,
            '1 Gi': 1073741824,
            '1 Ti': 1099511627776,
            '7 Ki': 7168,
            '7 Mi': 7340032,
            '7 Gi': 7516192768,
            '7 Ti': 7696581394432,
            '7.42423 Ki': 7602,
            '7.42423 Mi': 7784869,
            '7.42423 Gi': 7971706261,
            '7.42423 Ti': 8163027212283,
            '10 Ki': 10240,
            '10 Mi': 10485760,
            '10 Gi': 10737418240,
            '10 Ti': 10995116277760,
            '100 Ki': 102400,
            '100 Mi': 104857600,
            '100 Gi': 107374182400,
            '100 Ti': 109951162777600,
            '1000 Ki': 1024000,
            '1000 Mi': 1048576000,
            '1000 Gi': 1073741824000,
            '1000 Ti': 1099511627776000,
            '11234234 Ki': 11503855616,
            '11234234 Mi': 11779948150784,
            '11234234 Gi': 12062666906402816,
            '11234234 Ti': 12352170912156483584
        }

        results = {}
        for i in (0, 0.1, 0.5, 0.9, 1, 7, 7.42423, 10, 100, 1000, 11234234):
            for src_unit in ['Ki', 'Mi', 'Gi', 'Ti']:
                results[f'{i} {src_unit}'] = human2bytes(f'{i} {src_unit}')
        self.assertEqual(results, expected_results)
