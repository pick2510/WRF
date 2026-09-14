#!/usr/bin/env python3
"""Report how far each mirrored CPU routine moved between two revisions.

Why this exists
---------------
The GPU ports in this tree are additive: each ``*_gpu`` / ``*_fast`` routine
is a line-by-line transcription of a CPU routine, living in a module that
upstream WRF never touches.  That is what makes a version bump merge cleanly
-- and it is exactly why a clean merge proves nothing.  Any CPU routine that
changed between the two versions leaves its GPU mirror **silently stale**, with
no conflict and no compiler complaint.

So after every rebase or version bump, run this over the files that carry GPU
mirrors and read the "changed lines" column as a worklist.

Whitespace-only churn is ignored.  A routine wrapped in a macro layer that one
version has and the other does not (WRF's ``HYBRID_COORD`` block, deleted in
v4.7.1) will report a huge, misleading number -- pipe the older side through
``port/expand_hybrid.py`` first, then audit.  On dyn_em/module_small_step_em.F
that single step took ``advance_uv`` from 87 changed lines to 0.

Usage
-----
    python3 port/audit.py V3.9.1.1 v4.7.1 phys/module_ra_rrtmg_lw.F
    python3 port/audit.py V3.9.1.1 v4.7.1 dyn_em/module_small_step_em.F \\
                          --expand-old          # apply expand_hybrid to the old side

Follow up on anything non-zero with ``git diff`` on the routine, and on the
GPU mirrors with ``port/stmt_compare.py``.
"""

import difflib
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUB_START = re.compile(r'\s*SUBROUTINE\s+(\w+)', re.I)
SUB_END = re.compile(r'\s*END\s+SUBROUTINE', re.I)


def routines(text):
    """Split a Fortran source into {lowercase name: [lines]}.  Does not handle
    nested CONTAINS-ed subroutines; the ports hoist those to module scope
    anyway, which is a hard requirement for device code."""
    out, cur, buf = {}, None, []
    for line in text.split('\n'):
        if cur is None:
            m = SUB_START.match(line)
            if m:
                cur, buf = m.group(1).lower(), [line]
            continue
        buf.append(line)
        if SUB_END.match(line):
            out[cur] = buf
            cur = None
    return out


def show(rev, path):
    return subprocess.run(['git', 'show', '%s:%s' % (rev, path)],
                          capture_output=True, text=True, check=True).stdout


def expand(text):
    r = subprocess.run([sys.executable, os.path.join(HERE, 'expand_hybrid.py')],
                       input=text, capture_output=True, text=True, check=True)
    return r.stdout


def main(argv):
    expand_old = '--expand-old' in argv
    argv = [a for a in argv if a != '--expand-old']
    old_rev, new_rev, path = argv[0], argv[1], argv[2]

    old_text = show(old_rev, path)
    if expand_old:
        old_text = expand(old_text)
    old, new = routines(old_text), routines(show(new_rev, path))

    print('%s: %s -> %s%s\n' % (path, old_rev, new_rev,
                                '  (old side macro-expanded)' if expand_old else ''))
    print('%-34s %8s %8s %10s' % ('routine', 'old', 'new', 'changed'))
    print('-' * 64)
    for name in sorted(set(old) | set(new)):
        if name not in old:
            print('%-34s %8s %8d %10s' % (name, '-', len(new[name]), 'NEW'))
            continue
        if name not in new:
            print('%-34s %8d %8s %10s' % (name, len(old[name]), '-', 'GONE'))
            continue
        a = [' '.join(l.split()) for l in old[name] if l.split()]
        b = [' '.join(l.split()) for l in new[name] if l.split()]
        changed = sum(1 for l in difflib.unified_diff(a, b, n=0)
                      if l[:1] in '+-' and l[:3] not in ('+++', '---'))
        print('%-34s %8d %8d %10d' % (name, len(old[name]), len(new[name]), changed))


if __name__ == '__main__':
    main(sys.argv[1:])
