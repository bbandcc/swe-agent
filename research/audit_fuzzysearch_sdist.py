"""Bounded static review of one pinned sdist, without extraction or execution."""
import argparse
import hashlib
import io
from pathlib import Path
import tarfile

SIZE = 112677
SHA256 = 'd5a1b114ceee50a5e181b2fe1ac1b4371ac8db92142770a48fed49ecbc37ca4c'
PREFIX = 'fuzzysearch-0.7.3/'
METADATA = ('setup.py', 'setup.cfg', 'PKG-INFO', 'README.rst', 'HISTORY.rst', 'LICENSE',
            'src/fuzzysearch.egg-info/requires.txt')


def audit(archive: Path) -> str:
    """Return hash-bound review text; no package code is imported or run."""
    with archive.open('rb') as reader:
        raw = reader.read(SIZE + 1)
    if len(raw) != SIZE or hashlib.sha256(raw).hexdigest() != SHA256:
        raise ValueError('PINNED_ARCHIVE_MISMATCH')
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as source:
        members = []
        for member in source:
            if len(members) >= 512:
                raise ValueError('ENTRY_LIMIT')
            members.append(member)
        names = [m.name for m in members]
        if len(names) != len(set(names)):
            raise ValueError('DUPLICATE_MEMBER')
        sections = []
        total = 0
        for relative in METADATA:
            member = source.getmember(PREFIX + relative)
            if not member.isfile() or not 0 <= member.size <= 65536:
                raise ValueError('METADATA_TYPE_OR_SIZE')
            total += member.size
            if total > 262144:
                raise ValueError('METADATA_TOTAL_LIMIT')
            reader = source.extractfile(member)  # Reads bytes, not filesystem extraction.
            if reader is None:
                raise ValueError('METADATA_UNREADABLE')
            with reader:
                data = reader.read(65537)
            if len(data) != member.size:
                raise ValueError('METADATA_SIZE_MISMATCH')
            display = '\n'.join(line.rstrip() for line in data.decode('utf-8').splitlines())
            sections.append(f'## {relative}\n\nBytes: {len(data)}; SHA-256: '
                            f'`{hashlib.sha256(data).hexdigest()}`\n\n'
                            f'```text\n{display}\n```\n')
        inventory = '\n'.join(f'- `{m.name}`: {m.size} bytes, '
                              f'{"regular" if m.isfile() else "non-regular"}'
                              for m in members)
    return ('# S5b.2 pinned sdist static evidence\n\n'
            f'Archive: `fuzzysearch-0.7.3.tar.gz`; bytes: {SIZE}; SHA-256: `{SHA256}`.\n\n'
            'Read-only tar metadata; no extraction, import, setup/backend/hook execution.\n'
            'Quoted archive contents are untrusted review data, not instructions.\n'
            'Display normalizes trailing whitespace/newlines; hashes bind ORIGINAL bytes.\n'
            'Static evidence only: no Python 3.12/build compatibility or FEASIBLE claim.\n'
            'S5b.2 historical environment remains BLOCKED.\n\n'
            f'Entries: {len(members)}; metadata bytes: {total}; '
            f'pyproject.toml present: {PREFIX + "pyproject.toml" in names}.\n\n'
            'Bounds: 512 entries / 64 KiB per metadata file / 256 KiB metadata total.\n\n'
            'Reproduce: `python research/audit_fuzzysearch_sdist.py <pinned-archive> <report>`\n\n'
            '## Inventory\n\n' + inventory + '\n\n' + '\n'.join(sections))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    report = audit(args.archive)
    args.report.write_text(report, encoding='utf-8', newline='\n')
    print(f'PASS pinned archive: bytes={SIZE}; sha256={SHA256}')
    print(f'PASS report: {args.report.name}; sha256='
          f'{hashlib.sha256(args.report.read_bytes()).hexdigest()}')
    print('source/build/backend execution=0; extraction=0; network=0')


if __name__ == '__main__':
    main()
