from __future__ import absolute_import, division, print_function, unicode_literals
from past.builtins import str as oldstr

import sys

USING_PYTHON2 = True if sys.version_info < (3, 0) else False


def compat_str(s):
    if USING_PYTHON2:
        return oldstr(s)
    else:
        return s.decode('utf-8') if isinstance(s, bytes) else s
