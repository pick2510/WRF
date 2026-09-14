#!/usr/bin/env python3
"""Compare a restructured GPU kernel set against the CPU routine it mirrors.

Why a plain diff will not do
---------------------------
The ``*_fast`` routines in dyn_em are deliberate restructurings: loop nests are
split so that j becomes a parallel axis, and each per-j scratch row is promoted
to a j-indexed buffer (``dpxy(i,k)`` -> ``fast_dpxy3(i,k,j)``).  Nothing lines
up, so ``diff`` reports the whole routine and hides the one line that matters.

What survives restructuring is the *set of assignment statements*.  This
extracts them, normalises the promoted buffers back to their CPU names, and
reports the symmetric difference.  Everything that comes back is either an
intentional divergence you can name, or a bug.

This is what caught all four defects in the V3.9.1.1 acoustic kernels -- three
dropped ``c1h(k)`` hybrid-coordinate weights and one uninitialised loop index
-- none of which a whole-routine diff or a code read had found.  See CLAUDE.md,
"dyn_em forward-ported to v4.7.1".

Usage
-----
    python3 port/stmt_compare.py gpu_routine.f cpu_routine.f

Each side should be a single extracted subroutine.  Read the output as a
checklist: for every line, say out loud why it is allowed to differ.
"""

import collections
import re
import sys

# Promoted scratch buffers -> the CPU spelling they stand in for.  Extend this
# when a new routine promotes a new buffer; an unmapped buffer just shows up as
# a difference, which is noisy but never wrong.
SUBSTITUTIONS = [
    (r'fast_dpxy3\(i,k,j\)',        'dpxy(i,k)'),
    (r'fast_dpn3\(i,([^,]+),j\)',   r'dpn(i,\1)'),
    (r'fast_wdtn3\(i,([^,]+),j\)',  r'wdtn(i,\1)'),
    (r'fast_rhs3\(i,([^,]+),j\)',   r'rhs(i,\1)'),
    (r'fast_wdwn3\(i,([^,]+),j\)',  r'wdwn(i,\1)'),
    (r'fast_dvdxi3\(i,([^,]+),j\)', r'dvdxi(i,\1)'),
    (r'fast_dmdt2\(i,j\)',          'dmdt(i)'),
    (r'fast_msft_inv2\(i,j\)',      'msft_inv(i)'),
]

# Loop bounds, tile indices and other setup: same on both sides by construction
# and only noise here.
SETUP_LHS = re.compile(r'^(i_|j_|k_|dx|dy|pi|dampmag|hdepth|damp_enabled'
                       r'|phi_adv_z2|cof|lid_flag)', re.I)

SKIP_STMT = re.compile(r'(DO|END|ENDDO|IF|ELSE|SUBROUTINE|REAL|INTEGER|LOGICAL'
                       r'|TYPE|IMPLICIT|CALL|USE|\w+:)\b', re.I)


def statements(path):
    text = open(path, errors='replace').read()
    # Join continuations.  Drop any comment lines sitting *inside* a continued
    # statement first -- WRF has several, and joining around them instead of
    # through them truncates the statement and manufactures a false difference.
    text = re.sub(r'&[ \t]*\n(?:[ \t]*!.*\n)+', '&\n', text)
    text = re.sub(r'&[ \t]*\n\s*', ' ', text)
    out = []
    for line in text.split('\n'):
        line = '' if line.lstrip().startswith(('!', '#')) else line.split('!')[0]
        line = line.strip()
        if not line or SKIP_STMT.match(line) or '=' not in line:
            continue
        for pat, rep in SUBSTITUTIONS:
            line = re.sub(pat, rep, line)
        line = re.sub(r'\s+', '', line)
        if SETUP_LHS.match(line.split('=')[0]):
            continue
        out.append(line.lower())                    # macro case is gone by now
    return out


def main(a_path, b_path):
    a, b = statements(a_path), statements(b_path)
    ca, cb = collections.Counter(a), collections.Counter(b)
    rc = 0
    for label, seq, mine, theirs in (('only in ' + a_path, a, ca, cb),
                                     ('only in ' + b_path, b, cb, ca)):
        lines, shown = [], set()
        for s in seq:                       # keep source order; report each once
            if mine[s] > theirs.get(s, 0) and s not in shown:
                shown.add(s)
                lines.append(s)
        print('--- %s (%d) ---' % (label, len(lines)))
        for s in lines:
            print('   ' + s[:160])
            rc = 1
    if not rc:
        print('identical statement sets')
    return rc


if __name__ == '__main__':
    sys.exit(main(sys.argv[1], sys.argv[2]))
