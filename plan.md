# GPU Dynamic-Core Port Plan

> **Scope note.** This file documents the `dyn_em` acoustic-step phase, which
> is complete and stable on the V3.9.1.1 line. It has **not** been forward-
> ported to v4.7.1 — on this branch `WRF_GPU_DYN` is defined in the arch
> stanza but matches no source, which is harmless. The current work is the
> RRTMG radiation port; see `CLAUDE.md` for its status and conventions.

## Goal

Port selected WRF `dyn_em` acoustic-step work to NVIDIA GPUs with OpenACC while retaining an unmodified CPU execution path when `WRF_GPU_DYN` is not defined. Preserve numerical correctness before pursuing speedup.

## Current State

- NVIDIA HPC SDK OpenACC configuration is available in `arch/configure.defaults`
  (it was `arch/configure_new.defaults` on the V3.9.1.1 line).
- GPU-only compile flags are scoped in `dyn_em/Makefile`.
- `calc_p_rho` runs on the GPU with persistent data mapping in `solve_em`.
- `calc_coef_w` runs on the GPU and keeps `a`, `alpha`, and `gamma` resident.
- `advance_w` uses one explicit device region per call, with sequential vertical sweeps and vectorized horizontal loops. This replaced 3,360 tiny automatic-kernel launches with 42 device-region calls in the one-hour profile.
- The one-hour `em_b_wave` GPU regression completes successfully. The guarded source also compiles in CPU-only preprocessing mode.

## Next Implementation Steps

1. Reduce synchronization around `advance_w`. Port the immediately adjacent acoustic routines first, then keep their shared fields resident across calls.
3. Profile each change with `NVCOMPILER_ACC_TIME=1`; track kernel-launch count, device transfers, and elapsed time separately.
4. Port the next high-cost, data-adjacent dynamic-core routines only after profiling identifies a transfer-saving group.
5. Keep all directives inside `#ifdef WRF_GPU_DYN`; CPU code must remain valid without OpenACC flags.

## Validation Workflow

1. Build GPU: `./configure` menu 82 (dmpar) — menu 11 on the V3.9.1.1 line —
   then `./compile em_b_wave`. Prepend the NVHPC PATH and set
   `ulimit -s unlimited` first; see `CLAUDE.md`.
2. Run the `test/em_b_wave` case with one MPI rank. Use a short isolated namelist during iteration; run the full five-day case before merging.
3. Build CPU: drop `-DWRF_GPU_DYN -DWRF_GPU_RAD` from `ARCH_LOCAL` in the
   generated `configure.wrf`, `touch` the guarded sources and rebuild; run
   the same case. (Same compiler and flags, so the only variable is the
   macro — a separate CPU-configured tree is not the same control.)
4. Compare completion status, restart/output fields within agreed tolerances, and wall time. Record GPU, CPU, and profiling results in the change description.

## Exit Criteria

The GPU and CPU builds compile cleanly, both pass the full regression, numerical differences are understood and within tolerance, and the GPU version shows a repeatable improvement on a representative production-sized domain.
