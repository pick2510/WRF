# GPU Dynamic-Core Port Plan

## Goal

Port selected WRF `dyn_em` acoustic-step work to NVIDIA GPUs with OpenACC while retaining an unmodified CPU execution path when `WRF_GPU_DYN` is not defined. Preserve numerical correctness before pursuing speedup.

## Current State

- NVIDIA HPC SDK OpenACC configuration is available in `arch/configure_new.defaults`.
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

1. Build GPU: `./configure` menu 11, then `./compile em_b_wave`.
2. Run the `test/em_b_wave` case with one MPI rank. Use a short isolated namelist during iteration; run the full five-day case before merging.
3. Build CPU: `./configure` menu 15, then `./compile em_b_wave`; run the same case.
4. Compare completion status, restart/output fields within agreed tolerances, and wall time. Record GPU, CPU, and profiling results in the change description.

## Exit Criteria

The GPU and CPU builds compile cleanly, both pass the full regression, numerical differences are understood and within tolerance, and the GPU version shows a repeatable improvement on a representative production-sized domain.
