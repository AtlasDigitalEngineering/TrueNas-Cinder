#!/usr/bin/env python3
"""Refuse a workflow expression interpolated into a shell body (#81).

    python3 tools/check_workflows.py

GitHub substitutes `${{ ... }}` into a `run:` body as *text*, before any
shell sees it, so the value becomes part of the script rather than an
argument to it. A value containing a quote or a newline is then code.

`actionlint` catches this only for expressions it knows are untrusted --
`github.event.*` and friends. It says nothing about
`steps.<id>.outputs.*`, which is what this repo's workflows actually use,
and which is safe **because of where the value came from**. That
provenance is not visible at the point of use and is not stable: the next
expression added to a step inherits the shape without inheriting the
guarantee.

Passing values through `env:` is correct regardless of provenance, so the
rule is the simple one -- no interpolation into a script, ever:

    env:
      VERSION: ${{ steps.version.outputs.version }}
    run: |
      echo "${VERSION}"

Deliberately not a YAML parse. `tests/unit` runs on `requests` alone so
the common CI job stays fast, and adding PyYAML to that lane to check
three files would cost more than the scanning is worth. The shapes it has
to recognise are covered by `tests/unit/test_check_workflows.py`,
including the ones this line-based approach could plausibly get wrong.
"""

import pathlib
import re
import sys


EXPRESSION = re.compile(r'\$\{\{')

# `run:` introducing a block scalar -- `|`, `>`, and their chomping and
# indentation indicators (`|-`, `>+`, `|2`), optionally with a trailing
# comment. The comment matters: `run: | # build it` is a block, and a
# pattern that insists on end-of-line after the indicator treats it as an
# ordinary scalar, so the script that follows is never scanned at all.
#
# Group 1 runs to the start of `run:`, dash included, because that is the
# column a sibling key returns to. Capturing only the leading whitespace
# puts the block's floor two columns too far left for the compact
# `- run: |` form, and then the step's own `env:` is read as shell -- so
# the recommended fix gets reported as the defect.
BLOCK = re.compile(r'^(\s*(?:-\s+)?)run:\s*[|>][-+0-9]*\s*(?:#.*)?$')

# `run:` with the script on the same line. The lookahead has to reject
# exactly what BLOCK accepts, or the two disagree about which shape a
# line is.
INLINE = re.compile(
    r'^(\s*(?:-\s+)?)run:\s+(?![|>][-+0-9]*\s*(?:#.*)?$)(.+?)\s*$')


def offending_lines(text):
    """Find expressions interpolated into a `run:` body.

    Args:
        text: The workflow file's contents

    Returns:
        List of `(line_number, line)` for each offending line, 1-based
    """
    found = []
    block_indent = None

    for number, line in enumerate(text.split('\n'), 1):
        if block_indent is not None:
            blank = not line.strip()
            indent = len(line) - len(line.lstrip())
            if not blank and indent <= block_indent:
                block_indent = None          # the block ended here
            elif not blank:
                if EXPRESSION.search(line):
                    found.append((number, line.strip()))
                continue

        match = BLOCK.match(line)
        if match:
            # The column `run:` starts at, so a sibling key at the same
            # column closes the block and anything deeper is script.
            block_indent = len(match.group(1))
            continue

        match = INLINE.match(line)
        if match and EXPRESSION.search(match.group(2)):
            found.append((number, line.strip()))

    return found


def check(directory):
    """Report every offending line under a workflows directory.

    Args:
        directory: Path to `.github/workflows`

    Returns:
        List of `(path, line_number, line)`
    """
    problems = []
    for path in sorted(pathlib.Path(directory).glob('*.y*ml')):
        for number, line in offending_lines(path.read_text()):
            problems.append((path, number, line))
    return problems


def main():
    root = pathlib.Path(__file__).resolve().parent.parent
    workflows = root / '.github' / 'workflows'
    if not workflows.is_dir():
        print('no .github/workflows directory at %s' % workflows)
        return 1

    problems = check(workflows)
    for path, number, line in problems:
        print('%s:%d: expression interpolated into a run: body -- pass it '
              'through env: instead\n    %s'
              % (path.relative_to(root), number, line))

    if problems:
        print('\n%d offending line(s).' % len(problems))
        return 1
    print('no workflow expressions in shell bodies.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
