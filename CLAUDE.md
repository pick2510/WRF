# WRF GPU Porting — Project Knowledge

This file captures accumulated knowledge for the ongoing effort to port WRF's
RRTMG radiation scheme (and, earlier, `dyn_em`) to OpenACC/GPU. Read this
before resuming GPU-port work in this repo.

## Top-level priority

Profiling the real Hong Kong case (`/mnt/nvme/wrf_hk_gpu_d01`, `radt=1`,
`ra_lw_physics=4`, `ra_sw_physics=4` = RRTMG) showed **radiation is ~76% of
total wall-clock time**. Porting RRTMG-LW and RRTMG-SW to GPU is the current
top priority, superseding the earlier `dyn_em` OpenACC port (which is done
and stable — see "Prior work" below).

Full architectural analysis, phase plan, and rationale live in `plan.md` at
the repo root — read that for the "why" behind the sequencing. This file is
the "how": conventions, gotchas, and status.

## Current status (update this as work progresses)

### RRTMG-LW — fully ported and verified (module_ra_rrtmg_lw.F)
All behind `#ifdef WRF_GPU_RAD`, all additive (original CPU code untouched):
- `taumol_gpu` + `taugb1_gpu`..`taugb16_gpu` (commit `53bd32c2e`)
- `setcoef_gpu` + `laytrop_gpu` + `setcoef_bnd_gpu` + `setcoef_driver_gpu` (commit `53bd32c2e`)
- `cldprmc_gpu` + `cldprmc_prep_gpu` (commit `53bd32c2e`)
- `rtrnmc_gpu` + `rtrnmc_secdiff_gpu` + `rtrnmc_cldprep_gpu` + `rtrnmc_zero_gpu` + `rtrnmc_post_gpu` + `rtrnmc_driver_gpu` (commit `ef83bf348`)
- `mcica_subcol_lw_gpu` + `mcica_subcol_lw_prep_gpu` + `mcica_subcol_lw_driver_gpu`
  (commit `e0e7edc84`) — the McICA stochastic subcolumn cloud generator that
  runs *before* `cldprmc` in the real per-column call chain (not part of
  the `rrtmg_lw` AER driver itself — it's called directly by
  `RRTMG_LWRAD`). Supports all three overlap modes WRF's `icloud` namelist
  option can select (icld=1/2/3); `irng` stays restricted to 0 (kissvec)
  since that's hardcoded in `RRTMG_LWRAD`'s call, not namelist-controlled.
  Verified bit-exact (not just FP-noise-tolerance — kissvec's bitwise
  integer RNG update is exactly reproducible) against the CPU reference for
  all three overlap modes on a synthetic cloudy profile. This was ported
  specifically to unblock LW driver integration (see below).
- `inatm_layers_gpu` + `inatm_scale_gpu` + `inatm_reduce_gpu` +
  `inatm_aerosol_gpu` + `inatm_cloud_gpu` + `inatm_driver_gpu` (commit
  `bfccf4a99`) — the WRF/GCM-input-to-RRTMG conversion (`inatm`, part of
  `rrtmg_lw_rad`) that runs between `mcica_subcol_lw` and the `cldprmc`/
  `setcoef`/`taumol` chain: pressure/temperature transfer, vmr→molec/cm2
  column density (`coldry`), cross-section scaling, precipitable water
  vapor (`pwvcm`, the one genuine cross-layer reduction in this routine —
  handled with an OpenACC `reduction()` clause rather than folded into the
  per-layer kernel), and McICA cloud/aerosol array reindexing. No STOP/
  GOTO/internal-procedure hazards at all — purely elementwise. Verified
  bit-exact on every output field except `pwvcm` (~1.4e-6 relative, FP
  reassociation noise from the reduction's sum order) against the CPU
  reference on a synthetic profile. **Caught one real bug during
  verification**: `pz(iplon,0)` must be set *before* the per-layer kernel
  that computes `coldry` (layer 1's formula reads `pz(l-1)=pz(0)`) — it was
  initially only set in `inatm_reduce_gpu`, which runs *after*
  `inatm_layers_gpu` needs it, silently reading uninitialized device memory
  and producing wildly wrong (including negative) `coldry` values. Fixed
  by adding a small preliminary per-column kernel inside
  `inatm_layers_gpu` itself that sets `pz`/`tz` index 0 before the main
  layer loop. This is the same *general class* of bug as `vrtqdr_sw_gpu`'s
  missing boundary (a value the original computes/consumes in one specific
  order that a restructured kernel sequence must preserve) — worth
  actively checking for whenever splitting one CPU subroutine's logic
  across multiple GPU kernels: enumerate what each output is read by
  downstream code *before* deciding kernel call order, not just what each
  kernel produces.
- This completes every computational building block the real per-column
  LW call chain needs (`inatm` → `mcica_subcol_lw` → `cldprmc` →
  `setcoef` → `taumol` → `rtrnmc`), each independently GPU-ported and
  verified. **Driver integration** (wiring these into `RRTMG_LWRAD`'s
  actual column loop) has not been started — see below.

Kernel-level benchmark (15,876 columns, real Hong Kong domain):
CPU 4.04s vs GPU 0.161s = **25.1x speedup** on the full LW compute chain
(setcoef+taumol+cldprmc+rtrnmc).

### RRTMG-SW — fully ported and verified (module_ra_rrtmg_sw.F)
All behind `#ifdef WRF_GPU_RAD`, all additive:
- `cldprmc_sw_gpu` + `cldprmc_sw_prep_gpu` (commit `b5b7e3f7e`) — verified bit-exact
- `setcoef_sw_gpu` + `laytrop_sw_gpu` + `setcoef_sw_driver_gpu` (commit `f04c91e05`) — verified to ~1e-6 relative (FP noise)
- `taugb16_gpu`..`taugb29_gpu` (all 14 bands) + `laysolfr_upper_gpu` + `laysolfr_lower_gpu` + `taumol_sw_driver_gpu` (commit `ba497fb4c`) — verified to 2.3e-7 relative (FP noise)
- `reftra_sw_gpu` (elementwise, collapse(3) over column/layer/g-point) +
  `vrtqdr_sw_gpu` (genuine bottom-up/top-down recurrence, gang over
  column/g-point) + `spcvmc_sw_gpu`'s `spcvmc_sw_zero_gpu` /
  `spcvmc_sw_prep_gpu` / `spcvmc_sw_cumprod_gpu` / `spcvmc_sw_combine_gpu` /
  `spcvmc_sw_accumulate_gpu` / `spcvmc_sw_driver_gpu` (commit `3871aea4a`) —
  verified to ~1.2e-4 absolute / ~3e-7 relative (FP noise) on all flux
  fields (bbfd/bbfu/bbcd/bbcu) against a real dumped cloudy column. Two real
  bugs were caught and fixed during verification (see "OpenACC conventions"
  below for the general lesson): a missing surface (level `nlayers+1`)
  boundary condition on the *combined* total-sky `zref/zrefd/ztra/ztrad`
  arrays (the clear-sky and cloud-only sub-arrays had it; the weighted
  combination did not), and a missing `prup(klev+1)`/`prupd(klev+1)`
  boundary inside `vrtqdr_sw_gpu` itself — in the original `vrtqdr_sw`,
  `prup`/`prupd` are `intent(inout)` and the *caller* pre-sets index
  `klev+1` to the surface albedo before calling; once localized to
  per-thread arrays inside the GPU kernel, that caller-side pre-set had to
  be reproduced explicitly or the array's last (never-written) entry feeds
  garbage into the final flux computation. Both bugs were invisible in a
  quick read of the port and were only caught by comparing per-level output
  against the CPU reference one field and one layer at a time — worth
  remembering as the debugging technique when a whole-column diff looks
  "roughly right but off": isolate by field first (clear vs total, up vs
  down), then by layer, before suspecting the bulk kernel logic.

RRTMG-SW's radiative-transfer chain (`spcvmc_sw`) is now fully ported.

**LW's compute chain is now fully assembled and end-to-end verified as one
unit**: `rrtmg_lw_gpu_chain_driver` (module `rrtmg_lw_gpu_chain`, commit
`dc67ca887`) chains `mcica_subcol_lw_driver_gpu` → `inatm_driver_gpu` →
`cldprmc_gpu` → `setcoef_driver_gpu` → `taumol_gpu` → (new)
`lw_combine_taua_gpu` → `rtrnmc_driver_gpu` into a single ncol-batched
call, mirroring `RRTMG_LWRAD`'s per-column `mcica_subcol_lw` + `rrtmg_lw`
call pair exactly. Verified against a CPU reference that chains the
*original* `mcica_subcol_lw`+`rrtmg_lw` the same way, on a synthetic
cloudy profile: flux/heating-rate outputs match to ~1e-4 absolute on
values ~400 W/m2 (~2e-7 relative, FP-reassociation noise from the many
chained reductions — expected, not a bug). Two new pieces were needed and
are documented in the chain driver's header comment:
- `lw_combine_taua_gpu` — the missing elementwise `taug+taua` combination
  step (`rrtmg_lw`'s own "combine gaseous and aerosol optical depths",
  via the g-point→band map `ngb`) that sits between `taumol` and `rtrnmc`
  and had not been ported at all until this stage.
- `cldprmc_prep_batch_gpu` — a reformulation of `cldprmc_prep_gpu`'s
  host-side bound validation to run on **pre-mcica** per-column data
  (`cldfrac`/`ciwp`/`clwp`/`cswp`/`tauc`/`rei`/`rel`/`res`) instead of
  mcica's per-subcolumn stochastic output, which only exists on-device
  once mcica has run there — copying it back to host just for a bound
  check would be an expensive round-trip. This is an **exact**
  reformulation, not an approximation: every g-point in a band shares
  that band's `tauc`, so checking "does this band's tauc make validation
  necessary" (16 bands) reproduces the identical trigger condition as the
  original's per-g-point check (140 g-points), just grouped differently.
  Worth remembering as a general technique: when a host validation step
  depends on a device-computed intermediate, look for whether the
  validation's *trigger condition* can be re-derived from data that was
  already available before that intermediate was computed, rather than
  assuming a device→host round-trip is required.

**Cloud-optics "gap" investigated and closed — it isn't actually a gap.**
The earlier concern (a session or two ago) was that `RRTMG_LWRAD`'s inline
`taucld = abcw*clwp + abice*ciwp`-style empirical formula
(`module_ra_rrtmg_lw.F` around what was then line ~17844) computes `tauc`
and had never been ported. Reading the actual runtime code path resolved
this: `RRTMG_LWRAD` hardcodes `inflglw = 2` (bumped to 3/4/5 depending on
`has_reqc`/`has_reqi`, but never back to 0) *before* the `ICLOUD` check, and
that formula only executes `if (inflglw .eq. 0)` — dead code for every
actual call this project makes. For `inflglw > 0` (always, in practice),
`taucld` is explicitly host-zeroed (`! Zero out cloud optical properties
here; not used when passing physical properties to radiation and taucld is
calculated in radiation`) and the real cloud optical depth is instead
computed entirely *inside* `cldprmc`/`cldprmc_gpu` from `ciwpmc`/`clwpmc`/
`cswpmc`/`reicmc`/`relqmc`/`resnmc` via the iceflag/liqflag cascades —
already fully GPU-ported and verified (`cldprmc_gpu`, commit `53bd32c2e`).
So `rrtmg_lw_gpu_chain_driver`'s `tauc` input can simply stay a permanent
host-zeroed array; there is no cloud-optics kernel left to write for LW.

**Driver integration wiring — implemented, and it runs the real domain, but
hit a genuine bug at production scale that is NOT YET fixed.** `RRTMG_LWRAD`
now has a `#ifdef WRF_GPU_RAD` alternate path (added this session, pure
addition, original CPU per-column path untouched) that:
- Accumulates each column's already-computed inputs (`play`, `plev`,
  `tlay`, `tlev`, `tsfc`, the gas vmrs, `emis`, `cldfrac`, `ciwpth`/
  `clwpth`/`cswpth`, `rei`/`rel`/`res`, `tauaer`) into device-sized batch
  arrays instead of calling `mcica_subcol_lw`+`rrtmg_lw` per column. All of
  `RRTMG_LWRAD`'s existing host-side per-column setup (unit conversions,
  CAM-style effective-radius calcs, ozone interpolation, aerosol handling)
  is reused completely unmodified — only the two final calls and the
  output-writing that depended on their results were split into CPU/GPU
  branches.
- Flushes the chunk (once full, or at the tile's last column) via a new
  internal subroutine `gpu_flush_lw_chunk` (nested in `RRTMG_LWRAD` via
  `CONTAINS`, so it has host association to all the batch arrays and
  output arguments without needing a long parameter list) that calls
  `rrtmg_lw_gpu_chain_driver` once per chunk and unpacks
  `totuflux`/`totdflux`/`totuclfl`/`totdclfl`/`htr` back into `glw`, `olr`,
  `lwcf`, `lwupt`/`lwuptc`/`lwdnt`/`lwdntc`/`lwupb`/`lwupbc`/`lwdnb`/
  `lwdnbc`, `lwupflx`/`lwupflxc`/`lwdnflx`/`lwdnflxc`, and `rthratenlw` for
  every (i,j) in that chunk, using the same index mapping established and
  verified in `check_lw_chain.f90` (driver's 0-based `totuflux(:,0:nlayers)`
  vs. the original `rrtmg_lw`'s 1-based `uflx(:,k+1)`, and `htr` only
  spanning `0:nlayers-1`).
- Also added `gpu_tables_lw_todevice` (module `rrtmg_lw_gpu_tables`) and
  wired one call to it into `rrtmg_lwinit` (guarded by a `SAVE`d
  once-only flag, since `rrtmg_lwinit` can run once per domain) — this
  pushes every read-only LW lookup table to the device exactly once for
  the life of the run. **This had never been wired into the real build
  before**: every previous verification used a standalone harness that
  called this same table-push logic by hand
  (`/tmp/rrtmg_integrate/gpu_tables.f90`), so nothing had ever exercised
  the production `rrtmg_lwinit` call path. Forgetting it doesn't fail to
  compile — every GPU kernel that reads one of these tables aborts at run
  time with `FATAL ERROR: data in PRESENT clause was not found on device`.
- **Real bugs found and fixed while getting this far** (both are instances
  of the same underlying lesson — see the new bullet under "OpenACC
  conventions" below):
  1. `gpu_flush_lw_chunk` originally sliced the persistent, chunk-sized
     batch arrays (`gpu_play(1:gpu_nfill,:)`, etc.) directly into the chain
     driver call. Whenever `gpu_nfill < gpu_chunksize` (the tile's last,
     partial chunk), that slice is **non-contiguous** (Fortran arrays are
     column-major; slicing only the leading dimension skips memory between
     rows), so passing it to the chain driver's explicit-shape dummy
     arguments forced the compiler to generate a **hidden host-side
     copy-in/copy-out temporary** — a different memory address than
     whatever `!$acc enter data copyin` had just pushed. Every kernel
     inside then failed with `data in PRESENT clause was not found on
     device`. Fixed by always copying into freshly `ALLOCATE`d, exactly
     `(gpu_nfill,...)`-shaped (and therefore always contiguous) temporary
     arrays before doing any `!$acc enter data`/kernel call/`exit data` —
     mirroring what `check_lw_chain.f90` did all along with plain
     `ncol`-sized arrays, never a slice of something bigger.
  2. After fixing (1), the *second* chunk flush crashed with `variable in
     data clause is partially present on the device`. Cause: the `!$acc
     enter data create(...)` list for the new per-flush temporaries
     included `fl_htrc`, but the matching `!$acc exit data` list omitted
     it — so `fl_htrc`'s device mapping was never released. When its host
     array was `DEALLOCATE`d and Fortran's allocator handed that same
     address to a later flush's `fl_tauc`, the runtime found a stale
     device mapping at that address left over from the leaked `fl_htrc`
     and refused to create a new one over it. Fixed by adding `fl_htrc` to
     the `exit data delete(...)` clause. Worth a general lesson: whenever
     an object's device lifetime is scoped to one call of a subroutine
     that runs many times in a loop (rather than being pushed once at
     init), the create/copyin list and the delete/copyout list must be
     diffed name-for-name, not just eyeballed — a single missing name is
     invisible until host memory happens to get reused at that address on
     a *later* call, which can take several iterations to surface.
- **Still open**: after both fixes above, the real 1-rank Hong Kong case
  (`run_minutes=2` for a quick check) got through 2 full radiation calls
  (t=0 and t=00:25, both producing plausible-looking `dpsdt`/`dmudt`
  domain-average diagnostics) but crashed on the 3rd with a genuine device
  fault — `Accelerator Fatal Error: call to cuStreamSynchronize returned
  error 700 (CUDA_ERROR_ILLEGAL_ADDRESS)` inside `taugb1_gpu` — not a
  "data not present" error this time, an actual out-of-bounds/invalid
  device memory access during kernel execution. `gpu_flush_lw_chunk`'s own
  create/delete lists and `rrtmg_lw_gpu_chain_driver`'s internal
  create/delete lists were both re-audited name-for-name after this crash
  and are balanced (33/33 and matching, respectively), so this is very
  likely a **different** bug: either a genuine out-of-range `jp`/`jt`/
  `indself`/`indfor`/`indminor` index reaching `taugb1_gpu`'s table lookups
  for some column somewhere in the real 15,876-column domain that the
  single-synthetic-profile/single-dumped-column verification never
  exercised, or a subtler device-memory lifecycle issue in the
  repeated-every-timestep `ALLOCATE`/`enter data`/`exit data`/`DEALLOCATE`
  cycle that only manifests after several timesteps' worth of churn. Not
  yet root-caused — the next step is almost certainly running under
  `compute-sanitizer` (or `NVCOMPILER_ACC_...` debug env vars) to get the
  exact faulting address/column rather than guessing further from the
  Fortran source. Until this is fixed, `WRF_GPU_RAD`'s LW path is **not
  safe to run** on the real case beyond ~2 radiation calls; `run_minutes`
  has been restored to `10` in the namelist but the GPU path itself should
  be considered broken for real runs until this is resolved.
- **Chunk sizing is solved, device-independently**:
  `rrtmg_lw_gpu_chunksize(nlayers, max_ncol)` (module `rrtmg_lw_gpu_chunk`,
  commit `01df733b0`) queries the *actual running device's* free memory at
  runtime via OpenACC's `acc_get_property(devnum, acc_device_nvidia,
  acc_property_free_memory)` and divides by a per-column byte-footprint
  estimate (`rrtmg_lw_gpu_bytes_per_column`, currently ~0.34MB/column at
  nlayers=65) with a 50% safety margin for already-resident allocations
  (k-distribution tables, other OpenACC ports' working arrays) the query
  can't see. **This must never be replaced with a number hardcoded to any
  one GPU's VRAM capacity** — the whole point is that the same build sizes
  correctly on this project's 8GB RTX 5060 or on a very different device;
  querying `acc_get_property` at runtime is the established pattern for
  this, not a namelist option or a compile-time constant. Verified on this
  device: reports needing 2 chunks for the real 15,876-column Hong Kong
  tile at nlayers=65. The byte-per-column *estimate* should be revisited
  now that `rrtmg_lw_gpu_chain_driver`'s real array list is final (it
  wasn't, when the estimate was first written) — the sizing *mechanism* is
  solid, the number it multiplies is worth double-checking against the
  chain driver's actual `ALLOCATE` list.
- **Build wiring done, but differently than originally planned**: rather
  than dispatching between a CPU and a GPU version of `RRTMG_LWRAD` from
  `module_radiation_driver.F:1521` (the `dyn_em`-style `IF`/`ELSE` shape),
  `RRTMG_LWRAD` itself now branches internally via `#ifdef WRF_GPU_RAD`
  around its two innermost per-column calls (see above) — `module_
  radiation_driver.F` needed **no changes at all**, since it still just
  calls `RRTMG_LWRAD` exactly as before. Simpler than planned, and it means
  the CPU code path inside `RRTMG_LWRAD` truly never executes when
  `WRF_GPU_RAD` is defined, rather than living on as dead code.
- End-to-end verification against the *actual* RRTMG output fields
  (`RTHRATENLW`, `GLW`, `OLR`, `LWCF`, the `LWUPT`/`LWDNB`/etc. flux
  diagnostics) on a real multi-rank run is **blocked on the open bug
  above** — the real 1-rank run got 2 radiation calls in before crashing,
  not enough for a meaningful comparison against the CPU baseline yet.

SW driver integration (`RRTMG_SWRAD`) has not been started at all yet and
will need the same treatment once LW's is working — including its own
cloud-optics gap (SW's cloud optical depth/single-scattering-albedo/
asymmetry-parameter calculation, `RRTMG_SWRAD`'s own inline code, has not
even been located yet, let alone ported).

### Prior work (done, stable, not part of current focus)
`dyn_em` advection (`advect_u/v/w/scalar_o5v3`) and the acoustic loop
(`advance_uv/mu_t/w_fast`) are fully ported to OpenACC for the
`h_*_adv_order=5`/`v_*_adv_order=3` combination the Hong Kong case runs,
verified at 1/2/4 ranks, commits `f411e1135`..`ee73040e8`. GPU wall-clock is
at CPU parity. Diffusion (`module_diffusion_em.F`, `diff_opt=2`/`km_opt=4`)
was scoped but never started.

A **dormant CUDA Fortran port** also exists in the tree:
`phys/module_ra_rrtmg_lwf.F` (`ra_lw_physics=24`) and
`module_ra_rrtmg_swf.F` (`ra_sw_physics=24`) — pre-existing AER/Michalakes
work, `ncol`-major and already flattened, but targeting `cuda5.0,cc35`
(2013-era hardware) and never adapted for NVHPC 26.5/Blackwell. Used only as
a **design reference** for the restructuring shape, not as code to revive —
the user explicitly chose "straight phase 2... skip reviving the dormant
CUDA Fortran port" and go directly to a real OpenACC port of the reference
(`ra_lw_physics=4`/`ra_sw_physics=4`) code path.

## Hardware / toolchain

- GPU: RTX 5060, 8GB VRAM, `cc120` (Blackwell)
- Only NVHPC 26.5 installed, at `/mnt/nvme/hpc_sdk/Linux_x86_64/26.5`
- Build is `-r4` (single precision) — this matters for verification
  tolerances (see below)
- **PATH gotcha**: the default shell PATH resolves `mpifort` to a
  Homebrew/gfortran wrapper (`/usr/bin/mpifort` → linuxbrew gcc 16), NOT
  NVHPC's. Every build command must prepend:
  ```
  export PATH=/mnt/nvme/hpc_sdk/Linux_x86_64/26.5/compilers/bin:/mnt/nvme/hpc_sdk/Linux_x86_64/26.5/comm_libs/mpi/bin:$PATH
  ```
  Symptom if wrong: `gfortran: error: unrecognized command-line option '-acc'`
  (and `-Mfree`, `-byteswapio`, `-r4`, `-i4`, `-gpu=cc120`, `-Kieee`, `-module`)
  scattered through the compile log, and the build finishes suspiciously
  fast (~5-7s) with "Problems building executables" at the end.
- **Known-harmless build noise**: the `diffwrf` external tool (in
  `external/io_netcdf` and `external/io_int`) fails to link with
  `undefined reference to nf_strerror_` (and similar `nf_*` symbols) on
  *every* build, PATH issue or not — the Makefile marks it
  `Error 1 (ignored)`. Don't chase this; check for
  `--->  Executables successfully built  <---` at the end of the log instead
  of grepping for "error".
- **Memory**: the box has 62GB RAM. Builds normally use ~1-3GB and finish in
  2-3 minutes. If a build hangs and system memory drops toward zero with
  heavy swapping, something has gone wrong (e.g., a duplicate/stray build or
  WRF run left running) — check `free -h` and `pgrep -af "wrf.exe|nvfortran|
  mpifort|make -r"`, and `pkill -9` stale processes before retrying. This
  happened once this session and was unrelated to any code change.

## Build/verify methodology (established and repeatedly used)

For each new `_gpu` subroutine ported:

1. **Write the port** as a new `MODULE ..._gpu` block, guarded by
   `#ifdef WRF_GPU_RAD`, inserted immediately after the `end module` of the
   original CPU module it mirrors, in the same `.F` file. Never touch the
   original CPU code.
2. **Compile-check**: `rm` stale `.o`/`.f90`/`.G`/`.H`/`.bb` for the file,
   then `export J="-j 1"; ./compile em_real` in the background (with a
   Monitor loop watching `/proc/meminfo` and grepping the log for
   `Executables successfully built` — builds take 2-3 min). Fix any syntax
   errors before proceeding.
3. **Add a temporary dump macro**: pick a unique name like
   `WRF_GPU_RAD_DUMP_<STAGE>`, add it to `configure.wrf`'s `ARCH_LOCAL` line
   (alongside the existing `-DWRF_GPU_DYN -DWRF_GPU_RAD -DWRF_USE_CLM`).
4. **Add a dump `BLOCK`** right after the real CPU call site of the routine
   being ported, guarded by `#ifdef WRF_GPU_RAD_DUMP_<STAGE>` and a `SAVE`d
   `LOGICAL :: dumped = .FALSE.` fired-once flag. For routines with
   interesting-vs-trivial behavior (e.g. cloud routines where a clear-sky
   column is a boring test), gate the dump on a data-interestingness
   condition (`ANY(cswpmc>0)`, `ncbands>1`, etc.) so the captured column
   actually exercises the interesting code paths. Write inputs+outputs to an
   unformatted binary file with `OPEN(..., FORM='UNFORMATTED', STATUS='REPLACE')`.
5. **Rebuild**, then run the real case:
   `mpirun -np 1 ./wrf.exe` in `/mnt/nvme/wrf_hk_gpu_d01` (temporarily
   lowering `run_minutes` in `namelist.input` if the interesting case needs
   several minutes of simulated time to appear — restore to `10` after).
   Watch for the dump file to appear, then `pkill -9 -f wrf.exe` — no need to
   let the run finish.
6. **Copy the dump** to `/tmp/rrtmg_integrate/`, then **revert** the dump
   `BLOCK`, the `configure.wrf` macro, and `run_minutes`, and rebuild clean
   to confirm the revert compiles.
7. **Write a standalone `check_<stage>.f90` harness** in
   `/tmp/rrtmg_integrate/` (see "Reusable test infrastructure" below) that:
   - reads the dump
   - re-runs the *original* CPU routine on the same inputs as a self-check
     against the dumped CPU reference (should be bit-exact; if not, the dump
     itself is suspect, not the port)
   - runs the new `_gpu` routine with `ncol=1` on identical inputs
   - reports max absolute (and, for large-magnitude fields, max *relative*)
     differences
8. **Compile the harness**: link against `dm_stubs.o`, `gpu_tables.o`, the
   real `module_ra_rrtmg_lw.o`/`module_ra_rrtmg_sw.o` (and the other one too,
   if cross-references exist — SW pulls in some LW symbols, see below), and
   `module_wrf_error.o`. Use the exact same compiler flags as the real build
   (`-r4 -i4 -Mfree -byteswapio -acc -gpu=cc120 -Kieee`).
9. **Extend `gpu_tables.f90`** with any new lookup tables the port needs
   (`!$acc enter data copyin(...)`), and call the relevant
   `gpu_tables_*_todevice()` subroutine(s) at the top of the harness, before
   calling the `_gpu` routine. Missing table pushes show up as
   `FATAL ERROR: data in PRESENT clause was not found on device`, naming the
   exact missing array — just add it.

### Tolerance expectations

- Integer outputs (indices like `jp`, `jt`, `jt1`, `indself`, `indfor`,
  `laytrop`) and any array that's a straight table copy should be
  **bit-exact**.
- Floating-point outputs from the same arithmetic in different reduction
  order (e.g. atomic-add reorderings across g-points, or just compiler-level
  reassociation differences between scalar CPU code and a vectorized GPU
  kernel) will show small but real diffs at the **single-precision epsilon**
  level (~1.2e-7). When a raw absolute diff looks large, always also check
  the **relative** diff — a diff of 4e-3 on a value of 45692 is only 8.7e-8
  relative, i.e. exactly the expected noise floor, not a bug. This exact
  situation happened for `taumol_sw`'s `taug` and is expected.
- A genuine bug produces relative diffs orders of magnitude above single
  precision epsilon (1e-3, 1e-2, O(1), sign flips, etc.), or breaks the
  bit-exact integer/table fields.

## Reusable test infrastructure (`/tmp/rrtmg_integrate/`)

- `dm_stubs.f90` — minimal Fortran stand-ins for `wrf_dm_on_monitor`
  (`.TRUE.`), `wrf_dm_bcast_bytes`/`wrf_dm_bcast_real`/`wrf_dm_bcast_integer`
  (no-ops), `wrf_abort` (`STOP`), `wrf_debug` (no-op). Link ahead of
  `module_ra_rrtmg_lw.o`/`module_ra_rrtmg_sw.o`/`module_wrf_error.o` so these
  symbols resolve without pulling in the full RSL_LITE/MPI DM layer.
- `gpu_tables.f90` — shared device-table-residency helpers, one subroutine
  per logical group, all just chains of `!$acc enter data copyin(...)`:
  - `gpu_tables_todevice()` — all LW tables (rrlw_con/ref/wvn/tbl/cld, all 16
    `rrlw_kgNN` bands, `rtrnmc_gpu`'s `a0/a1/a2_gpu`)
  - `gpu_tables_sw_todevice()` — SW cloud-optics tables (`rrsw_cld`),
    `rrsw_wvn` (`ngb`/`wavenum1`/`wavenum2`/`delwave`), `rrsw_ref`
    (`pref`/`preflog`/`tref`)
  - `gpu_tables_sw_taumol_todevice()` — all 14 SW `rrsw_kgNN` bands' tables
    (`absa`/`absb`/`forref`/`selfref`/`sfluxref`/band-specific extras like
    `absch4`/`abso3a`/`rayla`/`raylb`/`absh2o`/`absco2`), plus
    `rrsw_con::oneminus` and `rrsw_wvn::nspa`/`nspb`
  - `gpu_tables_sw_spcvmc_todevice()` — just `rrsw_tbl::exp_tbl` (the
    exponential lookup table `reftra_sw_gpu`/`spcvmc_sw_prep_gpu`/
    `spcvmc_sw_combine_gpu` all use). `rrsw_tbl::bpade` is a plain module
    scalar, not a `parameter` — per the scalar-`present()` rule below, it is
    deliberately *not* pushed and *not* listed in any kernel's
    `present()` clause; it is captured by value (firstprivate) at each
    kernel launch instead, which is correct since it is set once at
    `rrtmg_sw_ini` time and never changes. `tblint`/`od_lo` are real
    `parameter`s and need no device handling at all.
  - Each band's tables are pulled in via renamed `use` (`absa_s16=>absa`,
    etc.) purely to avoid name collisions across the 14 `use` statements in
    one subroutine — the `enter data copyin` still operates on the real
    underlying module variable, so this aliasing has no effect on which
    kernel later finds it `present`.
  - When adding a new table push, remember: **only arrays need `present()`
    in a kernel — bare scalars (like a band's single `rayl` or `strrat`
    constant) should NOT be listed in `!$acc present(...)`** for `_gpu`
    kernels; they get auto-firstprivate'd by the compiler at kernel launch.
    Explicitly listing a scalar in `present()` forces a device-residency
    lookup that will fail with `FATAL ERROR: data in PRESENT clause was not
    found on device` unless it was separately pushed — this bit taumol28's
    `rayl` once (fixed by removing it from the present clause, not by
    pushing it).
- `check_*.f90` — one harness per verified stage (LW: `check_taumol.f90`,
  `check_setcoef.f90`, `check_cldprmc.f90`, `check_rtrnmc.f90`,
  `check_mcica_lw.f90`, `check_inatm.f90`, `check_chunksize.f90`,
  `check_lw_chain.f90` (+ `check_lw_chain_manual.f90`, a debugging
  companion, see below); SW: `check_cldprmc_sw.f90`, `check_setcoef_sw.f90`,
  `check_taumol_sw.f90`, `check_spcvmc_sw.f90`). Use these as templates for
  the next stage's harness.
  `check_mcica_lw.f90`/`check_inatm.f90`/`check_lw_chain.f90` differ from
  the others in that they do **not** use a real dumped column — their
  inputs (a pressure profile, a cloud-fraction band, water paths, gas
  mixing ratios) are simple enough to synthesize directly in the harness,
  which is faster to iterate on than the dump/rebuild/rerun cycle and is
  fine whenever the routine under test has no complex
  upstream-physics-derived inputs.
  **Easy-to-forget harness setup step, worth checking first when a CPU
  reference chain mysteriously returns all-zero output**: any harness that
  calls `taumol`/`taumol_gpu` (directly or via a chain like
  `rrtmg_lw_gpu_chain_driver`) must load the k-distribution absorption
  tables from `RRTMG_LW_DATA`/`RRTMG_SW_DATA` via the `lw_kgbNN`/`sw_kgbNN`
  calls *before* calling `rrtmg_lw_ini`/`rrtmg_sw_ini` — `rrtmg_*_ini`
  alone does not load them. Forgetting this doesn't error at compile or
  link time; it silently leaves `absa`/`absb`/`fracrefa`/etc. at zero,
  so `taug`/`fracs` (and therefore every downstream flux) come out exactly
  `0.0` — plausible-looking, not a crash, and easy to mistake for a real
  bug in whichever new driver you're testing (`check_lw_chain_manual.f90`
  exists specifically because this was misdiagnosed as a chain-driver bug
  before the missing `lw_kgbNN` calls were spotted).
  `check_spcvmc_sw.f90` is also the template for debugging a
  "roughly right but off" whole-array diff: it breaks the aggregate
  max-diff down into per-field (bbfd/bbfu/bbcd/bbcu) and, temporarily
  while debugging, per-layer diffs — this is what actually localized the
  two `spcvmc_sw_gpu` boundary-condition bugs to a single level (the
  surface) rather than a bulk kernel-logic error.
- `bench_rrtmg_lw.f90` — full-domain (15,876 column) kernel-level benchmark
  harness for the completed LW chain.
- `*_dump.bin` — captured real-column dumps, one per verified stage. Keep
  around for regression re-checks after refactoring.
- `RRTMG_LW_DATA` / `RRTMG_SW_DATA` — copy from `run/` into
  `/tmp/rrtmg_integrate/` when a harness needs to read the k-distribution
  data file directly (only `taumol`/`taumol_sw` harnesses need this, via
  `lw_kgbNN`/`sw_kgbNN`; `cldprmc`/`setcoef` stages only need
  `rrtmg_lw_ini`/`rrtmg_sw_ini` to populate their tables from in-source
  `DATA` statements).

Byteswap note: the real WRF build uses `-byteswapio`, so dump files are
big-endian. Harnesses **must** compile with `-byteswapio` too or reads will
fail ("attempt to read past end of file").

## OpenACC conventions (hard constraints, established during `dyn_em` port,
## reconfirmed during RRTMG)

- **No enclosing `!$acc data` / manually-delimited `!$acc parallel` +
  `!$acc end parallel` region spanning multiple subroutine calls.** This
  crashes NVHPC 26.5. Use standalone, auto-closing
  `!$acc parallel loop ... <body> ...` blocks only. For scratch arrays
  shared across a short sequence of calls, use unstructured
  `!$acc enter data create(...)` / `!$acc exit data delete(...)` pairs
  instead of a structured `!$acc data` region.
- **Never slice a persistent array into a subroutine call that also needs
  `present()` to find it, unless the slice is guaranteed contiguous.**
  Fortran array-section actual arguments passed to explicit-shape dummy
  arguments get a compiler-generated host-side copy-in/copy-out temporary
  whenever the section isn't contiguous (e.g. `arr(1:n,:)` when `n` is less
  than `arr`'s declared first-dimension extent — column-major storage means
  that skips memory between rows). That temporary is a different address
  than whatever `!$acc enter data` was told about, so every kernel called
  with it fails `data in PRESENT clause was not found on device`. This bit
  `gpu_flush_lw_chunk`'s handling of a tile's final, partial chunk (real
  bug, see the LW driver-integration status entry above) — fixed by always
  copying into freshly `ALLOCATE`d, exactly-shaped (and therefore always
  contiguous) temporaries before any `!$acc enter data`/kernel call/`exit
  data`, never slicing a bigger persistent array directly into the call.
- **When a subroutine's device-data lifetime is scoped to one call, but
  that subroutine runs many times in a loop, diff its `enter data`
  create/copyin list against its `exit data` delete/copyout list
  name-for-name** — don't eyeball them. A single array present in one list
  but missing from the other leaks that device mapping forever. The bug
  stays completely invisible until a *later* call's `ALLOCATE` happens to
  reuse that exact host address for a *different* array, at which point
  `!$acc enter data` on the new array fails with `variable in data clause
  is partially present on the device` — a confusing error that looks
  unrelated to the actual (long-since-executed) missing-delete call. This
  is exactly what happened with `fl_htrc` in `gpu_flush_lw_chunk`: created
  once, never deleted, and only surfaced as a crash on the *second* flush.
  `grep -oE` both lists and `comm -23`/`comm -13` them rather than reading
  by eye — that's how this one was actually found.
- **STOP/`wrf_error_fatal`/`WRITE(errmess,...)` calls cannot run on device.**
  Hoist all such physical-bounds checks into a host-side `*_prep_gpu`
  pre-validation subroutine that runs once before the device kernel and
  aborts on the host if any input is out of bounds. Established in
  `cldprmc_prep_gpu` (LW) and `cldprmc_sw_prep_gpu` (SW). When a check
  guards a value computed from an *already-validated* radius/table lookup
  (not the raw input), it's a documented judgment call whether to re-derive
  it on the host too or treat it as guaranteed-safe — see the header
  comments on `cldprmc_gpu`/`cldprmc_sw_gpu` for the reasoning either way;
  when a branch has *no* earlier bound to lean on (e.g. SW's iceflag=1
  Ebert-Curry path), the derived quantity must be recomputed and checked on
  the host too, not skipped.
- **`intent(inout)` scratch arrays that rely on a caller-set boundary value
  must have that boundary reproduced explicitly when localized into a GPU
  kernel's per-thread private arrays.** The original CPU code often passes
  a partially-pre-filled array into a subroutine (e.g. `vrtqdr_sw`'s
  `prup`/`prupd`, where the *caller* sets `prup(klev+1)=`surface albedo
  before the call, and `vrtqdr_sw` itself only ever fills indices
  `1..klev`). When such an array becomes a `private()` local inside a GPU
  kernel instead of a shared caller-provided array, that pre-set boundary
  vanishes and the array's untouched entry silently feeds garbage into
  anything that reads it later — this produced a real, hard-to-spot bug in
  `vrtqdr_sw_gpu` (see `rrtmg_sw_spcvmc_gpu`'s status entry above) that only
  showed up as a wrong value at the single surface level, everything else
  bit-for-bit correct. When porting a routine with `intent(inout)` array
  arguments, always check what the *caller* pre-sets before the call, not
  just what the subroutine itself writes.
- **Internal (`CONTAINS`-nested) procedures cannot take `!$acc routine` under
  NVHPC and cannot be called from a device kernel anyway if they rely on
  Fortran host association.** Every routine that reaches its caller's locals
  by host association (LW's `taugb1..16`, SW's `taumol16..29`) must be
  hoisted to module scope with an explicit argument list before any device
  work is possible. This is consistently the single largest mechanical cost
  per port stage.
- **GOTO-driven loops must become structured `DO` loops** with the branch
  condition tested per-cell instead of via a running/loop-carried state
  variable, wherever the branch only depends on quantities already known per
  cell (not a true recurrence). Two established techniques:
  - **troposphere/stratosphere split** (`laytrop`): compute a per-column
    scalar count via a tiny sequential-scan-per-column kernel (nlayers
    iterations, cheap), matching the physical fact that pressure decreases
    monotonically with layer index so "how many layers satisfy `plog>4.56`"
    equals "the layer index below which all layers satisfy it". Then every
    downstream kernel does a plain `IF (lay <= laytrop(iplon))` per cell
    instead of splitting into two loops. Established as `laytrop_gpu` (LW),
    reused verbatim as `laytrop_sw_gpu` (SW).
  - **"find the unique reference layer" scans** (SW's `laysolfr`, used to
    decide which layer's data populates a band's TOA solar flux table):
    same technique — a per-column tiny sequential scan kernel
    (`laysolfr_upper_gpu`/`laysolfr_lower_gpu`), NOT a true recurrence since
    each layer's test only reads its own and one neighbor's already-known
    value.
  - **A genuine sequential recurrence** (LW's `rtrnmc` up/down flux sweep) is
    the one case that must stay a `!$acc loop seq` over layers inside an
    outer `!$acc parallel loop gang` over columns (and, in `rtrnmc`'s case,
    g-points too, since down-then-up sweeps are independent per g-point).
    Cross-g-point accumulation within a band (`urad`/`drad`) is handled with
    `!$acc atomic update` into shared per-(column,layer) accumulators —
    valid because the underlying math is a plain commutative sum.
- **Fixed-size automatic arrays for `private()` clauses**: per-gang scratch
  arrays passed through `private()` need compile-time-constant bounds; size
  them to a module parameter (`mxlay=203` for both LW's `parrrtm` and SW's
  `parrrsw`) rather than a runtime-shaped array.
- **species-last reindexing**: any array with a leading "species" or
  "molecule" index in the original (`wkl(species,layer)`,
  `wx(species,layer)`) becomes trailing-index in the ncol-major port
  (`wkl(iplon,lay,species)`), consistently, across both LW and SW.
- **ncol-major everywhere**: every per-layer/per-g-point array becomes
  `(iplon, ...)`-leading. This is the foundational restructuring that makes
  the embarrassingly-parallel column axis into the outer parallel-loop
  dimension.
- **Never hardcode a column-batch/chunk size to one GPU's VRAM capacity.**
  This project developed against an 8GB RTX 5060, but the code must size
  correctly on whatever device it actually runs on. Query the *running
  device's* free memory at runtime instead:
  `use openacc; devnum = acc_get_device_num(acc_device_nvidia);
  free_bytes = acc_get_property(devnum, acc_device_nvidia,
  acc_property_free_memory)` (all from NVHPC's `openacc` module, no extra
  linking needed), then divide by a per-column byte-footprint estimate
  with a safety margin (50% used so far, to leave headroom for
  already-resident allocations — k-distribution tables, other ports'
  working arrays — that this query can't separately account for). See
  `rrtmg_lw_gpu_chunksize`/`rrtmg_lw_gpu_bytes_per_column` (module
  `rrtmg_lw_gpu_chunk`) for the established pattern.

## Git conventions for this work

- **Detached HEAD is expected and normal** for this entire line of work
  (confirmed via `git branch -a`: `* (HEAD detached from V3.9.1.1)`),
  matching the prior `dyn_em` commit chain. Not an error to fix.
- **Do not add attribution lines to commit messages or PR descriptions.**
- One commit per verified port stage (not one giant commit for a whole
  routine's worth of sub-pieces already committed separately, and not one
  commit per file-touch — group by "this is now verified and working").
- Before committing, check `git diff --stat` on the touched file to confirm
  it's a clean, pure addition (no accidental reformatting/whitespace churn
  from an add/revert dump-instrumentation cycle) and `git status --short` to
  make sure no unrelated pre-existing modified files (there's often a stray
  unrelated diff in `module_sf_sfclayrev.F` from before this work started —
  leave it alone, it's not part of this task) get swept in by an `add -A`.
  Never use `git add -A`; add the specific file(s).
- `configure.wrf` is gitignored — safe to add/remove the temporary
  `WRF_GPU_RAD_DUMP_*` macro there without it ever showing up in `git
  status`.

## Where things are

- Reference (target) source: `phys/module_ra_rrtmg_lw.F` (14,438+ lines),
  `phys/module_ra_rrtmg_sw.F` (12,287+ lines, growing as bands are added)
- Dormant CUDA Fortran design reference (not to be revived, read-only
  inspiration): `phys/module_ra_rrtmg_lwf.F`, `phys/module_ra_rrtmg_swf.F`
- Build flags: `configure.wrf`'s `ARCH_LOCAL` line (gitignored, currently
  `-DNONSTANDARD_SYSTEM_SUBR -DWRF_GPU_DYN -DWRF_GPU_RAD -DWRF_USE_CLM` with
  no dump macro active between sessions), `phys/Makefile`'s `GPU_RAD_FLAGS`
  stanza (tracked, committed), `arch/configure_new.defaults`'s GPU template
  (tracked, committed — remember the `-Kieee` fix if regenerating
  `configure.wrf` from scratch via `./configure`, since the shipped template
  is missing it and it was only hand-added to the live `configure.wrf`).
- Real case to test against: `/mnt/nvme/wrf_hk_gpu_d01` (126×126 domain,
  6.25km, 65 vertical layers, Hong Kong, March 2025 case). `namelist.input`
  there should be left at `run_minutes = 10` between sessions.
- Standalone test harnesses and dumps: `/tmp/rrtmg_integrate/` (not in git,
  ephemeral but worth preserving within a machine/session for regression
  re-checks).
