# Repository Guidelines

## Project Structure & Module Organization

This is WRF 3.9.1.1. The ARW/EM dynamic core lives in `dyn_em/`; alternate NMM and experimental cores are in `dyn_nmm/` and `dyn_exp/`. Physics is in `phys/`, driver and domain infrastructure are in `main/`, `frame/`, and `share/`. Registry inputs in `Registry/` generate state, configuration, and communication code during the build—do not hand-edit generated files in `frame/`, `inc/`, or `main/` unless they are maintained sources. External I/O libraries are under `external/`. Ideal-case inputs and smoke-test runs are in `test/`; production-style namelists are in `run/`.

## Build, Test, and Development Commands

Set `NETCDF` to a compatible NetCDF installation, then configure interactively:

```csh
./configure
./compile em_b_wave -j 4
```

The latter builds the EM ideal-case executables and links them into `test/em_b_wave/`. Run a smoke test there with `./ideal.exe` followed by `./wrf.exe`; use `mpirun -np N ./wrf.exe` for an MPI build. `./clean` removes objects and generated intermediates; `./clean -a` also removes `configure.wrf` and test/run artifacts, so use it deliberately. The build scripts are C shell scripts, not Bash scripts.

## Coding Style & Naming Conventions

Most core sources are preprocessed fixed/free-form Fortran (`.F`). Preserve the surrounding indentation, continuation style, uppercase Fortran keywords, and loop order (`j`, `k`, `i`, with `i` innermost). Keep routine names descriptive and lower-case with underscores, e.g. `advance_mu_t`. Put feature-specific code behind existing or narrowly scoped CPP flags. Changes to a Registry definition must include any necessary generated-code/build dependency updates.

## Testing Guidelines

Use the smallest applicable ideal test first—`test/em_b_wave` for EM dynamics changes. Compare `rsl.*`, `wrfout*`, conservation diagnostics, and restart behavior against a baseline. Exercise both serial and MPI paths for halo/boundary changes. For performance work, compile with `-DBENCH` and record the `solve_em` timer output; do not claim a speedup without a CPU reference using the same case and resolution.

## Commit & Pull Request Guidelines

Recent history uses concise imperative subjects such as `Bug-fix for diffusion slope term #304`. Keep commits narrowly scoped and name the affected subsystem. Pull requests should state the configuration/core tested, commands run, numerical comparison results, performance impact where relevant, and linked issue. Include generated Registry artifacts only when the project convention requires them; never commit build products, `configure.wrf`, or model output.
