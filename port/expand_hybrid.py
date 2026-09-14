#!/usr/bin/env python3
"""Expand WRF's HYBRID_COORD macro layer, preserving source formatting.

Why this exists
---------------
V3.9.1.1 implements the hybrid vertical coordinate with a layer of
function-like cpp macros at the top of dyn_em/module_small_step_em.F:
``mu(i,j)`` textually becomes ``(c1h(k)*mu(i,j))``, ``MUTHMUTF_KK`` becomes
``((c1h(k)*MUT(i,j)+c2h(k))*(c1f(k)*MUT(i,j)+c2f(k)))``, and so on.  v4.7.1
deleted the macros and wrote every expansion out by hand.

Diffing the two versions directly is therefore useless -- the routines look
almost entirely rewritten when in fact most of them are unchanged.  Expand
V3.9.1.1's source first and the real deltas collapse to almost nothing
(``calc_p_rho``, ``advance_uv`` and ``advance_mu_t`` come out identical).

``cpp`` can do the expansion, but ``cpp -P`` destroys blank lines and the
alignment of continued statements, which makes the output unusable as source
to port from.  This does the same substitution and leaves the layout alone.

The macros are CASE SENSITIVE, and the source exploits that: the uppercase
spellings (``MUT``, ``MU``, ``MUDF_XY``, ``DMDT``, ...) deliberately do not
match and pass through as the raw array.  So the same array can appear both
weighted and unweighted within a few lines, and only the lowercase/mixed-case
spelling carries the c1/c2 factor.  Do not "tidy" the case of anything here.

Usage
-----
    python3 port/expand_hybrid.py < old.F > expanded.F
    python3 port/expand_hybrid.py --check old.F     # agree with cpp?

``--check`` runs the real preprocessor over the same input with
``-DHYBRID_COORD=1`` and reports any line where the two disagree.  Run it
before trusting the output; it is the whole reason this file can be used as a
porting source rather than a guess.
"""

import re
import subprocess
import sys
import tempfile

# Macro table, transcribed from V3.9.1.1 dyn_em/module_small_step_em.F lines
# 1-53 (the `#if ( HYBRID_COORD==1 )` block).
FUNC_LIKE = {
    'mu':      '(c1h(k)*mu(%s))',
    'mut':     '(c1f(k)*mut(%s)+c2f(k))',
    'Mut':     '(c1h(k)*Mut(%s)+c2h(k))',
    'muu':     '(c1h(k)*muu(%s)+c2h(k))',
    'muv':     '(c1h(k)*muv(%s)+c2h(k))',
    'muave':   '(c1f(k)*muave(%s))',
    'Muave':   '(c1h(k)*Muave(%s))',
    'muus':    '(c1h(k)*muus(%s)+c2h(k))',
    'muvs':    '(c1h(k)*muvs(%s)+c2h(k))',
    'mu_tend': '(c1h(k)*mu_tend(%s))',
    'dmdt':    '(c1h(k)*dmdt(%s))',
    'muts':    '(c1f(k)*muts(%s)+c2f(k))',
    'Muts':    '(c1h(k)*Muts(%s)+c2h(k))',
    'mudf_xy': '(c1h(k)*mudf_xy(%s))',
}

OBJECT_LIKE = {
    'MUTHK':         '(c1h(k)*MUT(i,j)+c2h(k))',
    'MUTHKM1':       '(c1h(k-1)*MUT(i,j)+c2h(k-1))',
    'MUTHMUTF_KK':   '((c1h(k)*MUT(i,j)+c2h(k))*(c1f(k)*MUT(i,j)+c2f(k)))',
    'MUTHMUTF_KM1K': '((c1h(k-1)*MUT(i,j)+c2h(k-1))*(c1f(k)*MUT(i,j)+c2f(k)))',
    'MUTHMUTF_KKP1': '((c1h(k)*MUT(i,j)+c2h(k))*(c1f(k+1)*MUT(i,j)+c2f(k+1)))',
}

IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def expand_line(line):
    """Expand one line.  Like cpp, each macro expands exactly once: the
    replacement text names the macro again, which cpp's self-reference rule
    leaves alone, and so do we by emitting it directly rather than rescanning.

    Fortran comments are left alone.  cpp has no idea they are comments and
    will happily rewrite prose inside them (it only stops where it thinks a
    string literal has opened, which for Fortran source is arbitrary); mangled
    comments are no use to a human porting the code."""
    if line.lstrip().startswith('!'):
        return line
    out = []
    i = 0
    while i < len(line):
        m = IDENT.match(line, i)
        if not m:
            out.append(line[i])
            i += 1
            continue
        name, end = m.group(0), m.end()
        if name in OBJECT_LIKE:
            out.append(OBJECT_LIKE[name])
            i = end
            continue
        if name in FUNC_LIKE and end < len(line) and line[end] == '(':
            depth, k = 0, end
            while k < len(line):
                if line[k] == '(':
                    depth += 1
                elif line[k] == ')':
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            if k < len(line):                      # balanced on this line
                out.append(FUNC_LIKE[name] % line[end + 1:k])
                i = k + 1
                continue
        out.append(name)
        i = end
    return ''.join(out)


def resolve_conditionals(lines, defined):
    """Resolve the #if/#ifdef nesting in module_small_step_em.F.

    Deliberately refuses anything it does not recognise rather than guessing:
    a silently mis-taken branch is exactly the kind of error this tooling is
    meant to catch."""
    true_ifs = {'#if ( HYBRID_COORD==1 )'}
    false_ifs = {'#if ! ( HYBRID_COORD )', '#if ! ( HYBRID_COORD==1 )'}
    stack, out = [], []
    for n, raw in enumerate(lines, 1):
        t = raw.strip()
        # The macro block spells its directives "#  define", so normalise the
        # whitespace between the '#' and the keyword before matching.
        if t.startswith('#'):
            t = '#' + t[1:].lstrip()
        if t.startswith('#if'):
            if t in true_ifs:
                stack.append(True)
            elif t in false_ifs:
                stack.append(False)
            elif t.startswith('#ifdef '):
                stack.append(t.split(None, 1)[1] in defined)
            elif t.startswith('#ifndef '):
                stack.append(t.split(None, 1)[1] not in defined)
            else:
                sys.exit('line %d: unhandled directive %r' % (n, t))
            continue
        if t == '#else':
            stack[-1] = not stack[-1]
            continue
        if t == '#endif':
            stack.pop()
            continue
        if t.startswith(('#define', '#undef')):
            continue
        if all(stack):
            out.append(raw)
    return out


def check_against_cpp(path, defined):
    """Compare our expansion with the real preprocessor's."""
    ours = [l.rstrip('\n') for l in
            resolve_conditionals(open(path, errors='replace').read().split('\n'),
                                 defined)]
    ours = [expand_line(l) for l in ours]
    args = ['/lib/cpp', '-P', '-nostdinc', '-C', '-DHYBRID_COORD=1']
    args += ['-D%s' % d for d in defined]
    with tempfile.NamedTemporaryFile('w', suffix='.F') as tmp:
        tmp.write(open(path, errors='replace').read())
        tmp.flush()
        r = subprocess.run(args + [tmp.name], capture_output=True, text=True)
    theirs = [l for l in r.stdout.split('\n') if l.strip()]
    mine = [l for l in ours if l.strip()]
    # Compare with all whitespace removed: cpp collapses runs of spaces inside
    # a macro argument (``muv(i,j  )`` -> ``muv(i,j)``) and we deliberately do
    # not, since keeping the column alignment is the point of this script.
    # That difference is invisible to Fortran.
    # Comment lines are excluded: we leave them alone by design, cpp does not.
    squash = lambda s: ''.join(s.split())
    mine   = [l for l in mine   if not l.lstrip().startswith('!')]
    theirs = [l for l in theirs if not l.lstrip().startswith('!')]
    bad = 0
    for a, b in zip(mine, theirs):
        if squash(a) != squash(b):
            bad += 1
            if bad <= 10:
                print('  ours : %s\n  cpp  : %s\n' % (a.strip(), b.strip()))
    if len(mine) != len(theirs):
        print('line counts differ: ours %d, cpp %d' % (len(mine), len(theirs)))
        bad += 1
    print('%s: %d disagreements with cpp' % (path, bad))
    return 1 if bad else 0


if __name__ == '__main__':
    argv = sys.argv[1:]
    defined = {'WRF_GPU_DYN'}
    if argv and argv[0] == '--check':
        sys.exit(check_against_cpp(argv[1], defined))
    lines = resolve_conditionals(sys.stdin.read().split('\n'), defined)
    for line in lines:
        print(expand_line(line.rstrip('\n')))
