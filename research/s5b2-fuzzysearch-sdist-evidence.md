# S5b.2 pinned sdist static evidence

Archive: `fuzzysearch-0.7.3.tar.gz`; bytes: 112677; SHA-256: `d5a1b114ceee50a5e181b2fe1ac1b4371ac8db92142770a48fed49ecbc37ca4c`.

Read-only tar metadata; no extraction, import, setup/backend/hook execution.
Quoted archive contents are untrusted review data, not instructions.
Display normalizes trailing whitespace/newlines; hashes bind ORIGINAL bytes.
Static evidence only: no Python 3.12/build compatibility or FEASIBLE claim.
S5b.2 historical environment remains BLOCKED.

Entries: 39; metadata bytes: 27409; pyproject.toml present: False.

Bounds: 512 entries / 64 KiB per metadata file / 256 KiB metadata total.

Reproduce: `python research/audit_fuzzysearch_sdist.py <pinned-archive> <report>`

## Inventory

- `fuzzysearch-0.7.3`: 0 bytes, non-regular
- `fuzzysearch-0.7.3/AUTHORS.rst`: 152 bytes, regular
- `fuzzysearch-0.7.3/CONTRIBUTING.rst`: 3196 bytes, regular
- `fuzzysearch-0.7.3/HISTORY.rst`: 2780 bytes, regular
- `fuzzysearch-0.7.3/LICENSE`: 1080 bytes, regular
- `fuzzysearch-0.7.3/MANIFEST.in`: 332 bytes, regular
- `fuzzysearch-0.7.3/PKG-INFO`: 12150 bytes, regular
- `fuzzysearch-0.7.3/README.rst`: 6081 bytes, regular
- `fuzzysearch-0.7.3/setup.cfg`: 38 bytes, regular
- `fuzzysearch-0.7.3/setup.py`: 5268 bytes, regular
- `fuzzysearch-0.7.3/src`: 0 bytes, non-regular
- `fuzzysearch-0.7.3/src/fuzzysearch`: 0 bytes, non-regular
- `fuzzysearch-0.7.3/src/fuzzysearch/__init__.py`: 7866 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/_c_ext_base.h`: 824 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/_common.c`: 5796 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/_generic_search.c`: 362976 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/_levenshtein_ngrams.c`: 237463 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/_pymemmem.c`: 3047 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/_substitutions_only.c`: 3509 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/_substitutions_only_lp_template.h`: 3007 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/_substitutions_only_ngrams_template.h`: 4414 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/common.py`: 7375 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/compat.py`: 365 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/generic_search.py`: 11783 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/levenshtein.py`: 6945 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/levenshtein_ngram.py`: 7129 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/memmem.c`: 5508 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/memmem.h`: 464 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/no_deletions.py`: 5190 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/search_exact.py`: 2662 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/substitutions_only.py`: 11913 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/wordlen_memmem.c`: 10590 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch/wordlen_memmem.h`: 226 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch.egg-info`: 0 bytes, non-regular
- `fuzzysearch-0.7.3/src/fuzzysearch.egg-info/PKG-INFO`: 12150 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch.egg-info/SOURCES.txt`: 972 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch.egg-info/dependency_links.txt`: 1 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch.egg-info/requires.txt`: 12 bytes, regular
- `fuzzysearch-0.7.3/src/fuzzysearch.egg-info/top_level.txt`: 12 bytes, regular

## setup.py

Bytes: 5268; SHA-256: `1c226a8a3a4de0e6962e44196db2bc21cad5e6c0aa9a8aa43f3189f77034826f`

```text
#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import with_statement

import os
import sys

from setuptools import setup, Extension
from distutils.command.build_ext import build_ext
from distutils.errors import CCompilerError, DistutilsExecError, \
     DistutilsPlatformError

# --noexts: don't try building the C extensions
if '--noexts' in sys.argv[1:]:
    del sys.argv[sys.argv[1:].index('--noexts') + 1]
    noexts = True
else:
    noexts = False


def readfile(file_path):
    dir_path = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(dir_path, file_path), 'r') as f:
        return f.read()

readme = readfile('README.rst')
history = readfile('HISTORY.rst').replace('.. :changelog:', '')


# Fail safe compilation based on markupsafe's, which in turn was shamelessly
# stolen from the simplejson setup.py file.  Original author: Bob Ippolito

is_jython = 'java' in sys.platform
is_pypy = hasattr(sys, 'pypy_version_info')

ext_errors = (CCompilerError, DistutilsExecError, DistutilsPlatformError)
if sys.platform == 'win32' and sys.version_info > (2, 6):
    # 2.6's distutils.msvc9compiler can raise an IOError when failing to
    # find the compiler
    # It can also raise ValueError http://bugs.python.org/issue7511
    ext_errors += (IOError, ValueError)


class BuildFailed(Exception):
    pass


class ve_build_ext(build_ext):
    """This class allows C extension building to fail."""

    def run(self):
        try:
            build_ext.run(self)
        except DistutilsPlatformError:
            raise BuildFailed()

    def build_extension(self, ext):
        try:
            build_ext.build_extension(self, ext)
        except ext_errors:
            raise BuildFailed()
        except ValueError:
            # this can happen on Windows 64 bit, see Python issue 7511
            if "'path'" in str(sys.exc_info()[1]): # works with Python 2 and 3
                raise BuildFailed()
            raise


_substitutions_only_module = Extension(
    'fuzzysearch._substitutions_only',
    sources=['src/fuzzysearch/_substitutions_only.c',
             'src/fuzzysearch/memmem.c'],
    include_dirs=['.'],
)
_common_module = Extension(
    'fuzzysearch._common',
    sources=['src/fuzzysearch/_common.c',
             'src/fuzzysearch/memmem.c'],
    include_dirs=['.'],
)
_generic_search_module = Extension(
    'fuzzysearch._generic_search',
    sources=['src/fuzzysearch/_generic_search.c',
             'src/fuzzysearch/memmem.c'],
    include_dirs=['.'],
)
_levenshtein_ngrams_module = Extension(
    'fuzzysearch._levenshtein_ngrams',
    sources=['src/fuzzysearch/_levenshtein_ngrams.c'],
    include_dirs=['.'],
)
# pymemmem_module = Extension(
#     'fuzzysearch._pymemmem',
#     sources=['src/fuzzysearch/_pymemmem.c',
#              'src/fuzzysearch/memmem.c',
#              'src/fuzzysearch/wordlen_memmem.c'],
#     include_dirs=['.'],
# )


def run_setup(with_binary=True):
    ext_modules = [
        _substitutions_only_module,
        _common_module,
        _generic_search_module,
        _levenshtein_ngrams_module,
        # pymemmem_module,
    ]
    if not with_binary:
        ext_modules = []

    setup(
        name='fuzzysearch',
        version='0.7.3',
        description='fuzzysearch is useful for finding approximate subsequence matches',
        long_description=readme + '\n\n' + history,
        author='Tal Einat',
        author_email='taleinat@gmail.com',
        url='https://github.com/taleinat/fuzzysearch',
        packages=['fuzzysearch'],
        package_dir={'': 'src'},
        ext_modules=ext_modules,
        install_requires=['attrs>=19.3'],
        license='MIT',
        keywords='fuzzysearch',
        classifiers=[
            'Development Status :: 4 - Beta',
            'Intended Audience :: Developers',
            'License :: OSI Approved :: MIT License',
            'Natural Language :: English',
            'Programming Language :: Python :: 2',
            'Programming Language :: Python :: 2.7',
            'Programming Language :: Python :: 3',
            'Programming Language :: Python :: 3.5',
            'Programming Language :: Python :: 3.6',
            'Programming Language :: Python :: 3.7',
            'Programming Language :: Python :: 3.8',
            'Programming Language :: Python :: Implementation :: CPython',
            'Programming Language :: Python :: Implementation :: PyPy',
            'Topic :: Software Development :: Libraries :: Python Modules',
        ],
        cmdclass={'build_ext': ve_build_ext},
    )


def try_building_extension():
    try:
        run_setup(True)
    except BuildFailed:
        line = '=' * 74
        build_ext_warning = 'WARNING: The C extensions could not be ' \
                            'compiled; speedups are not enabled.'

        print(line)
        print(build_ext_warning)
        print('Failure information, if any, is above.')
        print('Retrying the build without the C extension now.')
        print('')

        run_setup(False)

        print(line)
        print(build_ext_warning)
        print('Plain-Python installation succeeded.')
        print(line)

if not (noexts or is_pypy or is_jython):
    try_building_extension()
else:
    run_setup(False)
```

## setup.cfg

Bytes: 38; SHA-256: `1c473cbaee8da5fc46e7f0158794af5cea4414c34a3cf3f180c2001f5e38bd3e`

```text
[egg_info]
tag_build =
tag_date = 0

```

## PKG-INFO

Bytes: 12150; SHA-256: `0e77c87c13790d2e1db50f414d21a23f96d130403fc291350619686dc537289b`

```text
Metadata-Version: 1.1
Name: fuzzysearch
Version: 0.7.3
Summary: fuzzysearch is useful for finding approximate subsequence matches
Home-page: https://github.com/taleinat/fuzzysearch
Author: Tal Einat
Author-email: taleinat@gmail.com
License: MIT
Description: ===========
        fuzzysearch
        ===========

        .. image:: https://img.shields.io/pypi/v/fuzzysearch.svg?style=flat
            :target: https://pypi.python.org/pypi/fuzzysearch
            :alt: Latest Version

        .. image:: https://img.shields.io/travis/taleinat/fuzzysearch.svg?branch=master
            :target: https://travis-ci.org/taleinat/fuzzysearch/branches
            :alt: Build & Tests Status

        .. image:: https://img.shields.io/coveralls/taleinat/fuzzysearch.svg?branch=master
            :target: https://coveralls.io/r/taleinat/fuzzysearch?branch=master
            :alt: Test Coverage

        .. image:: https://img.shields.io/pypi/wheel/fuzzysearch.svg?style=flat
            :target: https://pypi.python.org/pypi/fuzzysearch
            :alt: Wheels

        .. image:: https://img.shields.io/pypi/pyversions/fuzzysearch.svg?style=flat
            :target: https://pypi.python.org/pypi/fuzzysearch
            :alt: Supported Python versions

        .. image:: https://img.shields.io/pypi/implementation/fuzzysearch.svg?style=flat
            :target: https://pypi.python.org/pypi/fuzzysearch
            :alt: Supported Python implementations

        .. image:: https://img.shields.io/pypi/l/fuzzysearch.svg?style=flat
            :target: https://pypi.python.org/pypi/fuzzysearch/
            :alt: License

        Fuzzy search: Find parts of long text or data, allowing for some
        changes/typos.

        **Easy, fast, and just works!**

        .. code:: python

            >>> find_near_matches('PATTERN', '---PATERN---', max_l_dist=1)
            [Match(start=3, end=9, dist=1, matched="PATERN")]

        * Two simple functions to use: one for in-memory data and one for files

          * Fastest search algorithm is chosen automatically

        * Levenshtein Distance metric with configurable parameters

          * Separately configure the max. allowed distance, substitutions, deletions
            and/or insertions

        * Advanced algorithms with optional C and Cython optimizations

        * Properly handles Unicode; special optimizations for binary data

        * Simple installation:
           * ``pip install fuzzysearch`` just works
           * pure-Python fallbacks for compiled modules
           * only one dependency (``attrs``)

        * Extensively tested

        * Free software: `MIT license <LICENSE>`_

        For more info, see the `documentation <http://fuzzysearch.rtfd.org>`_.


        Installation
        ------------

        ``fuzzysearch`` supports Python versions 2.7 and 3.5+, as well as PyPy 2.7 and
        3.6.

        .. code::

            $ pip install fuzzysearch

        This will work even if installing the C and Cython extensions fails, using
        pure-Python fallbacks.


        Usage
        -----
        Just call ``find_near_matches()`` with the sub-sequence you're looking for,
        the sequence to search, and the matching parameters:

        .. code:: python

            >>> from fuzzysearch import find_near_matches
            # search for 'PATTERN' with a maximum Levenshtein Distance of 1
            >>> find_near_matches('PATTERN', '---PATERN---', max_l_dist=1)
            [Match(start=3, end=9, dist=1, matched="PATERN")]

        To search in a file, use ``find_near_matches_in_file()`` similarly:

        .. code:: python

            >>> from fuzzysearch import find_near_matches_in_file
            >>> with open('data_file', 'rb') as f:
            ...     find_near_matches_in_file(b'PATTERN', f, max_l_dist=1)
            [Match(start=3, end=9, dist=1, matched="PATERN")]


        Examples
        --------

        *fuzzysearch* is great for ad-hoc searches of genetic data, such as DNA or
        protein sequences, before reaching for "heavier", domain-specific tools like
        BioPython:

        .. code:: python

            >>> sequence = '''\
            GACTAGCACTGTAGGGATAACAATTTCACACAGGTGGACAATTACATTGAAAATCACAGATTGGTCACACACACA
            TTGGACATACATAGAAACACACACACATACATTAGATACGAACATAGAAACACACATTAGACGCGTACATAGACA
            CAAACACATTGACAGGCAGTTCAGATGATGACGCCCGACTGATACTCGCGTAGTCGTGGGAGGCAAGGCACACAG
            GGGATAGG'''
            >>> subsequence = 'TGCACTGTAGGGATAACAAT' # distance = 1
            >>> find_near_matches(subsequence, sequence, max_l_dist=2)
            [Match(start=3, end=24, dist=1, matched="TAGCACTGTAGGGATAACAAT")]

        BioPython sequences are also supported:

        .. code:: python

            >>> from Bio.Seq import Seq
            >>> from Bio.Alphabet import IUPAC
            >>> sequence = Seq('''\
            GACTAGCACTGTAGGGATAACAATTTCACACAGGTGGACAATTACATTGAAAATCACAGATTGGTCACACACACA
            TTGGACATACATAGAAACACACACACATACATTAGATACGAACATAGAAACACACATTAGACGCGTACATAGACA
            CAAACACATTGACAGGCAGTTCAGATGATGACGCCCGACTGATACTCGCGTAGTCGTGGGAGGCAAGGCACACAG
            GGGATAGG''', IUPAC.unambiguous_dna)
            >>> subsequence = Seq('TGCACTGTAGGGATAACAAT', IUPAC.unambiguous_dna)
            >>> find_near_matches(subsequence, sequence, max_l_dist=2)
            [Match(start=3, end=24, dist=1, matched="TAGCACTGTAGGGATAACAAT")]


        Matching Criteria
        -----------------
        The search function supports four possible match criteria, which may be
        supplied in any combination:

        * maximum Levenshtein distance (``max_l_dist``)

        * maximum # of subsitutions

        * maximum # of deletions ("delete" = skip a character in the sub-sequence)

        * maximum # of insertions ("insert" = skip a character in the sequence)

        Not supplying a criterion means that there is no limit for it. For this reason,
        one must always supply ``max_l_dist`` and/or all other criteria.

        .. code:: python

            >>> find_near_matches('PATTERN', '---PATERN---', max_l_dist=1)
            [Match(start=3, end=9, dist=1, matched="PATERN")]

            # this will not match since max-deletions is set to zero
            >>> find_near_matches('PATTERN', '---PATERN---', max_l_dist=1, max_deletions=0)
            []

            # note that a deletion + insertion may be combined to match a substution
            >>> find_near_matches('PATTERN', '---PAT-ERN---', max_deletions=1, max_insertions=1, max_substitutions=0)
            [Match(start=3, end=10, dist=1, matched="PAT-ERN")] # the Levenshtein distance is still 1

            # ... but deletion + insertion may also match other, non-substitution differences
            >>> find_near_matches('PATTERN', '---PATERRN---', max_deletions=1, max_insertions=1, max_substitutions=0)
            [Match(start=3, end=10, dist=2, matched="PATERRN")]


        When to Use Other Tools
        -----------------------

        * Use case: Search through a list of strings for almost-exactly matching
          strings. For example, searching through a list of names for possible slight
          variations of a certain name.

          Suggestion: Consider using `fuzzywuzzy <https://github.com/seatgeek/fuzzywuzzy>`_.




        History
        -------

        0.7.3 (2020-06-27)
        ++++++++++++++++++

        * Fixed segmentation faults due to wrong handling of inputs in bytes-like-only
          functions in C extensions.

        0.7.2 (2020-05-07)
        ++++++++++++++++++
        * Added PyPy support.
        * Several minor bug fixes.

        0.7.1 (2020-04-05)
        ++++++++++++++++++
        * Dropped support for Python 3.4.
        * Removed deprecation warning with Python 3.8.
        * Fixed a couple of nasty bugs.

        0.7.0 (2020-01-14)
        ++++++++++++++++++

        * Added ``matched`` attribue to ``Match`` objects containing the matched part
          of the sequence.
        * Added support for CPython 3.8. Now supporting CPython 2.7 and 3.4-3.8.

        0.6.2 (2019-04-22)
        ++++++++++++++++++

        * Fix calling ``search_exact()`` without passing ``end_index``.
        * Fix edge case: max. dist >= sub-sequence length.

        0.6.1 (2018-12-08)
        ++++++++++++++++++

        * Fixed some C compiler warnings for the C and Cython modules

        0.6.0 (2018-12-07)
        ++++++++++++++++++

        * Dropped support for Python versions 2.6, 3.2 and 3.3
        * Added support and testing for Python 3.7
        * Optimized the n-grams Levenshtein search for long sub-sequences
        * Further optimized the n-grams Levenshtein search
        * Cython versions of the optimized parts of the n-grams Levenshtein search

        0.5.0 (2017-09-05)
        ++++++++++++++++++

        * Fixed ``search_exact_byteslike()`` to support supplying start and end indexes
        * Added support for lists, tuples and other Sequence types to ``search_exact()``
        * Fixed a bug where ``find_near_matches()`` could return a wrong ``Match.end``
          with ``max_l_dist=0``
        * Added more tests and improved some existing ones.

        0.4.0 (2017-07-06)
        ++++++++++++++++++

        * Added support and testing for Python 3.5 and 3.6
        * Many small improvements to README, setup.py and CI testing

        0.3.0 (2015-02-12)
        ++++++++++++++++++

        * Added C extensions for several search functions as well as internal functions
        * Use C extensions if available, or pure-Python implementations otherwise
        * setup.py attempts to build C extensions, but installs without if build fails
        * Added ``--noexts`` setup.py option to avoid trying to build the C extensions
        * Greatly improved testing and coverage

        0.2.2 (2014-03-27)
        ++++++++++++++++++

        * Added support for searching through BioPython Seq objects
        * Added specialized search function allowing only subsitutions and insertions
        * Fixed several bugs

        0.2.1 (2014-03-14)
        ++++++++++++++++++

        * Fixed major match grouping bug

        0.2.0 (2013-03-13)
        ++++++++++++++++++

        * New utility function ``find_near_matches()`` for easier use
        * Additional documentation

        0.1.0 (2013-11-12)
        ++++++++++++++++++

        * Two working implementations
        * Extensive test suite; all tests passing
        * Full support for Python 2.6-2.7 and 3.1-3.3
        * Bumped status from Pre-Alpha to Alpha

        0.0.1 (2013-11-01)
        ++++++++++++++++++

        * First release on PyPI.
Keywords: fuzzysearch
Platform: UNKNOWN
Classifier: Development Status :: 4 - Beta
Classifier: Intended Audience :: Developers
Classifier: License :: OSI Approved :: MIT License
Classifier: Natural Language :: English
Classifier: Programming Language :: Python :: 2
Classifier: Programming Language :: Python :: 2.7
Classifier: Programming Language :: Python :: 3
Classifier: Programming Language :: Python :: 3.5
Classifier: Programming Language :: Python :: 3.6
Classifier: Programming Language :: Python :: 3.7
Classifier: Programming Language :: Python :: 3.8
Classifier: Programming Language :: Python :: Implementation :: CPython
Classifier: Programming Language :: Python :: Implementation :: PyPy
Classifier: Topic :: Software Development :: Libraries :: Python Modules
```

## README.rst

Bytes: 6081; SHA-256: `d4f7b88dcac63e9b6fbbbb7cc26ac03fbc5d4ac14f891d132d94435c5a71fc18`

```text
===========
fuzzysearch
===========

.. image:: https://img.shields.io/pypi/v/fuzzysearch.svg?style=flat
    :target: https://pypi.python.org/pypi/fuzzysearch
    :alt: Latest Version

.. image:: https://img.shields.io/travis/taleinat/fuzzysearch.svg?branch=master
    :target: https://travis-ci.org/taleinat/fuzzysearch/branches
    :alt: Build & Tests Status

.. image:: https://img.shields.io/coveralls/taleinat/fuzzysearch.svg?branch=master
    :target: https://coveralls.io/r/taleinat/fuzzysearch?branch=master
    :alt: Test Coverage

.. image:: https://img.shields.io/pypi/wheel/fuzzysearch.svg?style=flat
    :target: https://pypi.python.org/pypi/fuzzysearch
    :alt: Wheels

.. image:: https://img.shields.io/pypi/pyversions/fuzzysearch.svg?style=flat
    :target: https://pypi.python.org/pypi/fuzzysearch
    :alt: Supported Python versions

.. image:: https://img.shields.io/pypi/implementation/fuzzysearch.svg?style=flat
    :target: https://pypi.python.org/pypi/fuzzysearch
    :alt: Supported Python implementations

.. image:: https://img.shields.io/pypi/l/fuzzysearch.svg?style=flat
    :target: https://pypi.python.org/pypi/fuzzysearch/
    :alt: License

Fuzzy search: Find parts of long text or data, allowing for some
changes/typos.

**Easy, fast, and just works!**

.. code:: python

    >>> find_near_matches('PATTERN', '---PATERN---', max_l_dist=1)
    [Match(start=3, end=9, dist=1, matched="PATERN")]

* Two simple functions to use: one for in-memory data and one for files

  * Fastest search algorithm is chosen automatically

* Levenshtein Distance metric with configurable parameters

  * Separately configure the max. allowed distance, substitutions, deletions
    and/or insertions

* Advanced algorithms with optional C and Cython optimizations

* Properly handles Unicode; special optimizations for binary data

* Simple installation:
   * ``pip install fuzzysearch`` just works
   * pure-Python fallbacks for compiled modules
   * only one dependency (``attrs``)

* Extensively tested

* Free software: `MIT license <LICENSE>`_

For more info, see the `documentation <http://fuzzysearch.rtfd.org>`_.


Installation
------------

``fuzzysearch`` supports Python versions 2.7 and 3.5+, as well as PyPy 2.7 and
3.6.

.. code::

    $ pip install fuzzysearch

This will work even if installing the C and Cython extensions fails, using
pure-Python fallbacks.


Usage
-----
Just call ``find_near_matches()`` with the sub-sequence you're looking for,
the sequence to search, and the matching parameters:

.. code:: python

    >>> from fuzzysearch import find_near_matches
    # search for 'PATTERN' with a maximum Levenshtein Distance of 1
    >>> find_near_matches('PATTERN', '---PATERN---', max_l_dist=1)
    [Match(start=3, end=9, dist=1, matched="PATERN")]

To search in a file, use ``find_near_matches_in_file()`` similarly:

.. code:: python

    >>> from fuzzysearch import find_near_matches_in_file
    >>> with open('data_file', 'rb') as f:
    ...     find_near_matches_in_file(b'PATTERN', f, max_l_dist=1)
    [Match(start=3, end=9, dist=1, matched="PATERN")]


Examples
--------

*fuzzysearch* is great for ad-hoc searches of genetic data, such as DNA or
protein sequences, before reaching for "heavier", domain-specific tools like
BioPython:

.. code:: python

    >>> sequence = '''\
    GACTAGCACTGTAGGGATAACAATTTCACACAGGTGGACAATTACATTGAAAATCACAGATTGGTCACACACACA
    TTGGACATACATAGAAACACACACACATACATTAGATACGAACATAGAAACACACATTAGACGCGTACATAGACA
    CAAACACATTGACAGGCAGTTCAGATGATGACGCCCGACTGATACTCGCGTAGTCGTGGGAGGCAAGGCACACAG
    GGGATAGG'''
    >>> subsequence = 'TGCACTGTAGGGATAACAAT' # distance = 1
    >>> find_near_matches(subsequence, sequence, max_l_dist=2)
    [Match(start=3, end=24, dist=1, matched="TAGCACTGTAGGGATAACAAT")]

BioPython sequences are also supported:

.. code:: python

    >>> from Bio.Seq import Seq
    >>> from Bio.Alphabet import IUPAC
    >>> sequence = Seq('''\
    GACTAGCACTGTAGGGATAACAATTTCACACAGGTGGACAATTACATTGAAAATCACAGATTGGTCACACACACA
    TTGGACATACATAGAAACACACACACATACATTAGATACGAACATAGAAACACACATTAGACGCGTACATAGACA
    CAAACACATTGACAGGCAGTTCAGATGATGACGCCCGACTGATACTCGCGTAGTCGTGGGAGGCAAGGCACACAG
    GGGATAGG''', IUPAC.unambiguous_dna)
    >>> subsequence = Seq('TGCACTGTAGGGATAACAAT', IUPAC.unambiguous_dna)
    >>> find_near_matches(subsequence, sequence, max_l_dist=2)
    [Match(start=3, end=24, dist=1, matched="TAGCACTGTAGGGATAACAAT")]


Matching Criteria
-----------------
The search function supports four possible match criteria, which may be
supplied in any combination:

* maximum Levenshtein distance (``max_l_dist``)

* maximum # of subsitutions

* maximum # of deletions ("delete" = skip a character in the sub-sequence)

* maximum # of insertions ("insert" = skip a character in the sequence)

Not supplying a criterion means that there is no limit for it. For this reason,
one must always supply ``max_l_dist`` and/or all other criteria.

.. code:: python

    >>> find_near_matches('PATTERN', '---PATERN---', max_l_dist=1)
    [Match(start=3, end=9, dist=1, matched="PATERN")]

    # this will not match since max-deletions is set to zero
    >>> find_near_matches('PATTERN', '---PATERN---', max_l_dist=1, max_deletions=0)
    []

    # note that a deletion + insertion may be combined to match a substution
    >>> find_near_matches('PATTERN', '---PAT-ERN---', max_deletions=1, max_insertions=1, max_substitutions=0)
    [Match(start=3, end=10, dist=1, matched="PAT-ERN")] # the Levenshtein distance is still 1

    # ... but deletion + insertion may also match other, non-substitution differences
    >>> find_near_matches('PATTERN', '---PATERRN---', max_deletions=1, max_insertions=1, max_substitutions=0)
    [Match(start=3, end=10, dist=2, matched="PATERRN")]


When to Use Other Tools
-----------------------

* Use case: Search through a list of strings for almost-exactly matching
  strings. For example, searching through a list of names for possible slight
  variations of a certain name.

  Suggestion: Consider using `fuzzywuzzy <https://github.com/seatgeek/fuzzywuzzy>`_.
```

## HISTORY.rst

Bytes: 2780; SHA-256: `a42f846efd4997bc8db64d86d62b4c4c155e54ccdaf5ec850e0541b9747633ba`

```text
.. :changelog:

History
-------

0.7.3 (2020-06-27)
++++++++++++++++++

* Fixed segmentation faults due to wrong handling of inputs in bytes-like-only
  functions in C extensions.

0.7.2 (2020-05-07)
++++++++++++++++++
* Added PyPy support.
* Several minor bug fixes.

0.7.1 (2020-04-05)
++++++++++++++++++
* Dropped support for Python 3.4.
* Removed deprecation warning with Python 3.8.
* Fixed a couple of nasty bugs.

0.7.0 (2020-01-14)
++++++++++++++++++

* Added ``matched`` attribue to ``Match`` objects containing the matched part
  of the sequence.
* Added support for CPython 3.8. Now supporting CPython 2.7 and 3.4-3.8.

0.6.2 (2019-04-22)
++++++++++++++++++

* Fix calling ``search_exact()`` without passing ``end_index``.
* Fix edge case: max. dist >= sub-sequence length.

0.6.1 (2018-12-08)
++++++++++++++++++

* Fixed some C compiler warnings for the C and Cython modules

0.6.0 (2018-12-07)
++++++++++++++++++

* Dropped support for Python versions 2.6, 3.2 and 3.3
* Added support and testing for Python 3.7
* Optimized the n-grams Levenshtein search for long sub-sequences
* Further optimized the n-grams Levenshtein search
* Cython versions of the optimized parts of the n-grams Levenshtein search

0.5.0 (2017-09-05)
++++++++++++++++++

* Fixed ``search_exact_byteslike()`` to support supplying start and end indexes
* Added support for lists, tuples and other Sequence types to ``search_exact()``
* Fixed a bug where ``find_near_matches()`` could return a wrong ``Match.end``
  with ``max_l_dist=0``
* Added more tests and improved some existing ones.

0.4.0 (2017-07-06)
++++++++++++++++++

* Added support and testing for Python 3.5 and 3.6
* Many small improvements to README, setup.py and CI testing

0.3.0 (2015-02-12)
++++++++++++++++++

* Added C extensions for several search functions as well as internal functions
* Use C extensions if available, or pure-Python implementations otherwise
* setup.py attempts to build C extensions, but installs without if build fails
* Added ``--noexts`` setup.py option to avoid trying to build the C extensions
* Greatly improved testing and coverage

0.2.2 (2014-03-27)
++++++++++++++++++

* Added support for searching through BioPython Seq objects
* Added specialized search function allowing only subsitutions and insertions
* Fixed several bugs

0.2.1 (2014-03-14)
++++++++++++++++++

* Fixed major match grouping bug

0.2.0 (2013-03-13)
++++++++++++++++++

* New utility function ``find_near_matches()`` for easier use
* Additional documentation

0.1.0 (2013-11-12)
++++++++++++++++++

* Two working implementations
* Extensive test suite; all tests passing
* Full support for Python 2.6-2.7 and 3.1-3.3
* Bumped status from Pre-Alpha to Alpha

0.0.1 (2013-11-01)
++++++++++++++++++

* First release on PyPI.
```

## LICENSE

Bytes: 1080; SHA-256: `f694d09996c9b5600351c0c12492cea45de74e498ff06b03ce4e3784b4d8100d`

```text
The MIT License (MIT)

Copyright (c) 2013-2020 taleinat

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

## src/fuzzysearch.egg-info/requires.txt

Bytes: 12; SHA-256: `8aeff26ec8b3c245c7a3b8d33fe282044a0941ed4b3e62e2dfbc731311c08618`

```text
attrs>=19.3
```
