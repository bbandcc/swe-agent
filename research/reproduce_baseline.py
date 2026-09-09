"""Isolated source-level diagnostics; no model calls or project imports.

Fetch the pinned developer/graph.py into .research-cache/agent/developer/graph.py
before running. Executes selected original functions with model responses stubbed.
This is a diagnostic for the unmodified baseline, not an end-to-end benchmark.
"""

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace as NS


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / '.research-cache/agent/developer/graph.py'
SHA = '5946af4f57cba03761015837ad5f87ef5c8d99e9'


def load_original_functions():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    names = {'creating_diffs_for_task', 'prepare_for_implementation'}
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {
        'os': os, 're': re, 'SoftwareDeveloperState': NS, 'Diffs': NS,
        'JsonOutputParser': lambda **_: NS(get_format_instructions=lambda: ''),
        'convert_tools_messages_to_ai_and_human': lambda messages: messages,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace


def change(first, last, old, new):
    snippet = f'{first}| {old}' if first == last else f'{first}| {old}\n{last}| {old}'
    return f'<code_change_request>original_code_snippet: {snippet}\nedit_code_snippet: {new}</code_change_request>'


def probe_edit(namespace, directory, name, response, expected):
    path = directory / f'{name}.py'
    path.write_text('A\nB\nC\nD\n', encoding='utf-8')
    task = NS(file_path=str(path), atomic_tasks=[NS(atomic_task='edit', additional_context='')])
    state = NS(implementation_plan=NS(tasks=[task]), current_task_idx=0,
               current_atomic_task_idx=0, atomic_implementation_research=[])
    namespace['extract_diff_runnable'] = NS(invoke=lambda _: response)
    result = namespace['creating_diffs_for_task'](state)
    actual = path.read_text(encoding='utf-8')
    assert actual == expected, (name, actual, expected)
    return {'case': name, 'node_return': result, 'actual_content': actual}


def main():
    namespace = load_original_functions()
    # All writes are restricted to a temporary directory inside the research cache.
    with tempfile.TemporaryDirectory(dir=ROOT / '.research-cache') as tmp:
        directory = Path(tmp)
        cases = [
            probe_edit(namespace, directory, 'stale_line_numbers',
                       change(1, 1, 'A', 'A1\nA2') + change(4, 4, 'D', 'D_new'),
                       'A1\nA2\nB\nD_new\nD'),
            probe_edit(namespace, directory, 'original_text_mismatch',
                       change(2, 2, 'THIS_IS_NOT_B', 'REPLACED'), 'A\nREPLACED\nC\nD'),
            probe_edit(namespace, directory, 'no_parseable_edits',
                       'Model output without edit blocks', 'A\nB\nC\nD\n'),
        ]
        try:
            namespace['prepare_for_implementation'](NS(implementation_plan=NS(tasks=[]), current_task_idx=0))
        except IndexError:
            cases.append({'case': 'empty_plan', 'error': 'IndexError'})
        else:
            raise AssertionError('Expected baseline empty-plan failure')
    result = {
        'repository': 'langtalks/swe-agent', 'commit': SHA,
        'source_sha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        'method': 'Original function AST, mocked model response, temporary files; no graph runtime',
        'confirmed_cases': cases,
    }
    output = ROOT / 'research/baseline-diagnostics.json'
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Confirmed {len(cases)} baseline behaviors; result: {output}')


if __name__ == '__main__':
    main()
