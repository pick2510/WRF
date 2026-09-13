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

**Driver integration wiring — implemented, working, and verified on the real
domain.** `RRTMG_LWRAD`
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
- **`CUDA_ERROR_ILLEGAL_ADDRESS` crash — root-caused and fixed** (commit
  `202f94469`). Debugging sequence, since each tool ruled out a different
  cause:
  1. `compute-sanitizer --tool memcheck` on the real 1-rank case ran
     **clean (0 errors)** and the run completed — but this didn't mean the
     bug was gone, it meant memcheck's own allocator doesn't reuse
     freed host/device addresses the way glibc/the CUDA driver normally
     do, so an address-reuse-dependent bug simply never gets triggered
     under it. Don't treat a clean memcheck run as proof of correctness
     for this class of bug.
  2. `CUDA_LAUNCH_BLOCKING=1` on the plain (non-sanitizer) build still
     crashed, identically (same simulated time, same function/line,
     `CUDA_ERROR_INVALID_ADDRESS_SPACE` instead of `_ILLEGAL_ADDRESS` this
     time — a launch-time failure, not an execution-time one). This ruled
     out an async/missing-synchronization race as the cause.
  3. Redesigning `gpu_flush_lw_chunk` to push its batch arrays to the
     device once per `RRTMG_LWRAD` call (via `!$acc update device`/`update
     host` on persistent arrays) instead of `ALLOCATE`/`enter data`/`exit
     data`/`DEALLOCATE` on every flush did **not** fix the crash either —
     same time, same function/line again. This ruled out the repeated
     create/delete churn as the cause (though the redesign was kept: it's
     simpler and more robust regardless).
  4. A targeted host-side diagnostic (temporarily printing any column
     with `play`/`tlay` outside physically sane bounds, right before
     device dispatch) found the actual cause: **`play` (and `plev`) were
     genuinely `NaN`** for the affected columns, while `tlay`/`tlev` at
     the same columns/layers were finite. This is a **pre-existing bug**,
     not something this port introduced — the `dpsdt`/`dmudt`
     domain-average diagnostic WRF itself prints has been `NaN` from the
     very first timestep in this same run, independent of `WRF_GPU_RAD`.
     The original CPU `rrtmg_lw` path never crashed on it only by luck:
     x86's REAL→INTEGER conversion of `NaN` (`int(...)`) yields `INT_MIN`,
     which `setcoef`'s existing `IF (jp.lt.1) jp=1`-style bounds clamps
     happen to catch; the GPU's conversion of the same NaN-derived
     expression does not reliably land somewhere those same clamps catch,
     so an out-of-range `jp`/`jt`/`indself`/`indfor`/`indminor` reached a
     table lookup in `taugb1_gpu` and produced an invalid device address.
     **General lesson**: a GPU port can silently change behavior on inputs
     that were already undefined/garbage on the CPU side, even when the
     port's arithmetic is bit-for-bit identical — REAL→INTEGER conversion
     of `NaN`/`Inf` is implementation-defined and not guaranteed to match
     between CPU and GPU codegen. Don't assume "the CPU path never
     crashed on this" means "this input never occurs" — it may just mean
     the CPU's specific undefined-behavior outcome happened to be caught
     by a downstream clamp.
  5. **First fix attempt (incomplete, caused a second real bug)**:
     `gpu_flush_lw_chunk` clamped any non-finite/out-of-physical-range
     `play`/`plev`/`tlay`/`tlev`/`tsfc` to a single nominal value
     (1013.25 mb / 288 K) for every layer, immediately before device
     dispatch, then still computed and wrote that column's heating rate
     normally. This stopped the crash, but a 10-minute real run showed
     `dpsdt`/`dmudt` growing from `NaN` at t=0 to huge (thousands) and
     increasingly unstable values, with the model's adaptive timestep
     visibly shrinking (25.0s → 19.0s), and the warning firing for an
     ever-growing share of the domain (**15,625 of 15,876 columns** by the
     end). This looked like it might be a separate, serious, pre-existing
     instability — until a direct CPU-baseline comparison (same case,
     `/mnt/nvme/wrf_hk_cpu_d01`, `run_minutes=10`) showed **no such
     instability at all**: `dpsdt`/`dmudt` stayed finite and settled down
     over the run. Rebuilding with `WRF_GPU_RAD` *undefined* (keeping
     `WRF_GPU_DYN`, so radiation reverts to plain CPU `rrtmg_lw`) matched
     the CPU baseline almost exactly too. **This proved the instability was
     introduced by this session's RRTMG-LW GPU code, not a pre-existing
     dyn_em issue** — don't take "the CPU path is fine" as proof a GPU
     port hasn't regressed something; check the actual isolating
     variable (here: build with the new macro on vs. off) rather than
     assuming. The real mechanism, once found: clamping every layer of a
     bad column to one nominal surface-like pressure/temperature and then
     computing that column's LW heating rate as normal produces a heating
     rate that is *finite but physically absurd* for any layer that isn't
     actually near the surface (a real ~50 mb layer silently treated as
     1013 mb has wildly wrong optical path and therefore wildly wrong
     heating). That heating rate was still being written into
     `RTHRATENLW`, feeding a real, physical, self-reinforcing bug: bad
     heating → destabilized dynamics next step → more columns with
     NaN pressure → more bad clamped heating → worse instability. Not
     memory corruption at all, just wrong (but finite, so nothing crashed
     on it) physics being genuinely fed back into the model.
  6. **Actual fix**: added a per-column `gpu_bad(:)` flag, set alongside
     the clamp. `gpu_flush_lw_chunk`'s unpack loop now `CYCLE`s a flagged
     column entirely — `RTHRATENLW`/`GLW`/`OLR`/`LWCF`/etc. (all
     `INTENT(INOUT)`) simply keep whatever value they already had, rather
     than receiving a finite-but-wrong update. The clamp itself is still
     applied first (so the device kernels never see a NaN and can't
     propagate it through a per-column reduction like `pwvcm`), but its
     result is now only used to keep that one flush's kernel launch
     numerically well-behaved, never written back to the real output
     arrays.
  7. **Superseded — see point 8.** Point 6's `gpu_bad` skip did let a
     10-minute run reach `SUCCESS COMPLETE WRF` with `dpsdt`/`dmudt` near
     the CPU baseline, but the warning was still firing for 15,620 of
     15,625 columns every call, which should have been read as "the port is
     still broken and the skip is hiding it" rather than "fixed".
  8. **ROOT-CAUSED AND FIXED** (commit `0c1f32692`). Everything above about
     the NaN originating upstream in `dyn_em`/`phy_prep` was **wrong**, and
     the correction matters more than the bug:
     - **The prior diagnosis and how it went wrong.** The claim was that
       `grid%p_hyd`/`p_hyd_w` arrive `NaN` from `dyn_em` and radiation is a
       victim. It came from comparing two one-shot `SAVE`d diagnostic flags
       — an "entry" probe that fires on the very first call against a
       "detect-NaN" probe that fires on whatever *later* call first sees
       `NaN`. Comparing those two manufactures a "corrupted within one
       call" story out of two different calls. **Treat any conclusion drawn
       from two independently-`SAVE`d fired-once flags as unsound** unless
       both are keyed to the same call.
     - **How it was actually localized.** Probe the pipeline in *causal
       order*, in one run, with counts rather than first-hit prints:
       `RTHRATENLW` after radiation (`NaN`) → moisture/`p_hyd` before
       `phy_prep` (clean) → per-column `htr` straight out of
       `gpu_flush_lw_chunk` (943 of 9,534 columns `NaN`, **and a different
       943 on the next identical run**) → `COUNT(.NOT. finite)` on every
       intermediate of `rrtmg_lw_gpu_chain_driver` after each stage. That
       last step named `selffac`/`forfac`/`scaleminorn2` in `setcoef`, which
       pointed straight at `water = wkl/coldry` and at `coldry` itself.
       The run-to-run variation was the decisive clue: identical inputs,
       different results ⇒ a race or an uninitialized read, never data.
     - **Bug 1: `rtrnmc_gpu` zeroed only index 0** of its per-thread
       `urad`/`drad`/`clrurad`/`clrdrad(0:mxlay)` accumulators. The CPU
       `rtrnmc` zeroes all of `0:nlayers` before its band loop; once the
       g-point loop became one thread per `(column,g-point)` those arrays
       became thread-private and the full zeroing had to be reproduced.
       Every `drad(lev-1) = drad(lev-1) + radld` and
       `urad(lev) = urad(lev) + radlu` therefore accumulated onto
       uninitialized private memory, and `drad(nlayers)`/`clrdrad(nlayers)`
       — written by neither sweep — were read purely out of garbage.
       ~9% of columns.
     - **Bug 2: an intra-kernel race in `inatm_layers_gpu`.** Its
       `collapse(2)` kernel read `pz(iplon,l-1)` while writing
       `pz(iplon,l)` — a value produced by a *different thread of the same
       kernel*, with nothing ordering the two. ~0.3% of `(column,layer)`
       cells read `pz(iplon,l-1)` before it was written, giving wildly
       wrong `coldry` (usually negative, occasionally exactly zero), and
       zero made `water = wkl/coldry` a `0/0` `NaN`. Fixed by reading
       `plev(iplon,l)` instead, which is identical by construction and is a
       read-only input.
     - **The whole NaN cascade was a closed loop bootstrapped by
       radiation**: `NaN` heating → `RTHRATEN` → `t_tendf` → `t_tend` →
       `t_2` → the acoustic step → `mu`/`muts`, `al`/`alt` → `p_hyd` → back
       into the pressure `RRTMG_LWRAD` sanitizes. Note also that
       `RTHRATENSW` was `NaN` purely derivatively: `module_radiation_driver`
       passes `RTHRATENLW=RTHRATEN` to `RRTMG_LWRAD` and later computes
       `RTHRATENSW = RTHRATEN - RTHRATENLW`, so a `NaN` from LW shows up in
       both. SW was never involved.
  9. **Verified end-to-end** (this is the real verification the port needed,
     and the pattern to reuse for SW): full 10-minute real Hong Kong runs
     at 1, 2 and 4 ranks all reach `SUCCESS COMPLETE WRF` with **zero**
     "non-physical p/T" warnings (was 15,620), and every chain intermediate
     — `coldry`, `pwvcm`, `planklay`, `fac00`, `selffac`, `taug`, `taut`,
     `htr` — is finite for all 15,625 columns. Final-step domain-average
     `dpsdt`/`dmudt` against the CPU-only build:

     | build | ranks | dpsdt / dmudt |
     |---|---|---|
     | CPU-only | 1 | 103.6147 / 94.7676 |
     | GPU (dyn + LW rad) | 1 | 103.7536 / 94.7677 |
     | GPU (dyn + LW rad) | 2 | 103.6916 / 94.7661 |
     | GPU (dyn + LW rad) | 4 | 103.6548 / 94.7673 |

     `dmudt` agrees to 5 significant figures at every rank count, `dpsdt`
     to within 0.13%. An earlier, narrower check also holds: the *same*
     binary rebuilt with `WRF_GPU_RAD` undefined (CPU radiation, GPU
     dynamics) tracks the `WRF_GPU_RAD` build to 5 significant figures,
     which isolates radiation specifically.

     **Retraction, worth keeping because it nearly became doctrine**: for
     part of this session the GPU build showed a ~5% `dmudt` offset against
     the CPU baseline, and that was written up here as "caused by
     `WRF_GPU_DYN`, pre-existing, not the radiation port's problem". That
     was wrong. It was caused by a `solve_em.F` change made the same day
     (commit `030f4aa3a`, reverted in `dcca4e8e1`) that added an
     `!$acc update device` of `ph_2/al/p/mu_2/muts/mudf` after the
     small-step `set_physical_bc*` calls. The staleness runs the other
     way: during the acoustic loop the **device** holds the authoritative
     copies and the **host** ones are stale, except where a halo exchange
     has already forced an `update self` — which is exactly the
     `ntasks>1 .or. periodic` case the pre-existing push is conditioned on.
     At 1 rank there is no halo exchange, so that added push overwrote good
     device data with stale host data every substep. It was invisible at 4
     ranks because the pre-existing push fires there and made the new block
     redundant — **which is why it got misattributed to a build-level
     difference.** Two lessons: when a discrepancy appears, suspect what
     changed today before blaming a subsystem that has been stable for
     weeks; and a bug that disappears at higher rank counts is a hint about
     *which code path is conditional on rank count*, not evidence that the
     bug is environmental.
  10. **Wall clock**, real Hong Kong case, 10 minutes simulated, same
     machine (40-core box + RTX 5060), `nvfortran -O3` throughout:

     | build | ranks | wall |
     |---|---|---|
     | CPU-only (`WRF_cpu`) | 1 | 312.0s |
     | `WRF_GPU_DYN` only (CPU radiation) | 1 | 304.6s |
     | `WRF_GPU_DYN` + `WRF_GPU_RAD` | 1 | 245.1s |
     | `WRF_GPU_DYN` + `WRF_GPU_RAD` | 2 | 144.9s |
     | `WRF_GPU_DYN` + `WRF_GPU_RAD` | 4 | **85.4s** |
     | CPU-only | 4 | 100.6s |
     | CPU-only | 8 | 57.3s |

     At 1 rank the LW port is worth **1.27x** end-to-end, essentially all
     of it from radiation (the dyn port is at parity, as documented). The
     GPU build now scales across ranks sharing the one card (2.9x from 1 to
     4 ranks) and at 4 ranks it beats the 4-rank CPU build (85.4s vs
     100.6s) — but 8-rank CPU is still faster than anything the GPU build
     currently reaches, so **do not quote a headline GPU-vs-CPU number**.
     The remaining lever is SW radiation: it is still entirely on the CPU
     and is now the dominant radiation cost.

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
  **Made rank-aware** (commit `6004292d8`).
  `acc_get_property(..., acc_property_free_memory)` reports the whole
  device, so every rank believed it was alone and `-np 4` on the 8GB card
  died with `CUDA_ERROR_OUT_OF_MEMORY` before the first timestep.
  `rrtmg_lw_gpu_ranks_per_device()` now divides by the number of ranks
  actually sharing this rank's GPU, measured rather than assumed: split the
  domain communicator with `MPI_COMM_TYPE_SHARED` (per physical node),
  allgather each node-local rank's `acc_get_device_num`, count the matches.
  Correct for one GPU shared by all ranks, several GPUs with ranks spread
  over them, or one rank per GPU — no hardcoded device count. Cached, since
  `MPI_Comm_split_type` is collective and this is reached once per
  `RRTMG_LWRAD` call; every rank in `local_communicator` calls radiation on
  the same timesteps, so they execute the uncached path together once.

  **And the byte estimate was ~1.75x too low** — the TODO above is now
  done. Dividing by rank count alone got 2 ranks working but left 4 ranks
  failing at `cuLaunchKernel` rather than `cuMemAlloc`: the chunk allocation
  succeeded and the device then ran out reserving local memory for
  `rtrnmc_gpu`'s per-thread private arrays (8 arrays of `mxlay=203` reals,
  ~6.5KB per thread, reserved for every resident thread). Counting
  `rrtmg_lw_gpu_bytes_per_column` off the chain driver's real `ALLOCATE`
  list — 16 `ngptlw`-wide arrays, not 9, plus ~95 per-layer scalars that
  are not negligible once the g-point arrays are right — shrank the chunk
  enough that 4 ranks fits. **`safety_fraction` has to cover the CUDA
  context and that kernel local-memory reservation too**, neither of which
  a free-memory query taken before the first launch can see.
- **Build wiring done, but differently than originally planned**: rather
  than dispatching between a CPU and a GPU version of `RRTMG_LWRAD` from
  `module_radiation_driver.F:1521` (the `dyn_em`-style `IF`/`ELSE` shape),
  `RRTMG_LWRAD` itself now branches internally via `#ifdef WRF_GPU_RAD`
  around its two innermost per-column calls (see above) — `module_
  radiation_driver.F` needed **no changes at all**, since it still just
  calls `RRTMG_LWRAD` exactly as before. Simpler than planned, and it means
  the CPU code path inside `RRTMG_LWRAD` truly never executes when
  `WRF_GPU_RAD` is defined, rather than living on as dead code.
- End-to-end verification: done at 1, 2 and 4 ranks (see points 8–10
  above — `dmudt` matches the CPU-only build to 5 significant figures at
  every rank count over a full 10-minute run).
  Done at 2 and 4 ranks too (see point 9's table). **Still outstanding**: a
  direct field-level comparison of `RTHRATENLW`/`GLW`/`OLR`/`LWCF` and the
  `LWUPT`/`LWDNB` flux diagnostics. The Hong Kong namelist's `history_interval = 30`
  means a 10-minute run only ever writes the t=0 frame, which predates
  any radiation call — so raise `run_minutes` past 30, or drop
  `history_interval`, before attempting that comparison.

SW driver integration (`RRTMG_SWRAD`) has not been started and is now the
whole remaining lever — LW is done and SW is the dominant radiation cost.
What is and is not left, checked against the source rather than assumed:

- **Ported and individually verified already**: `cldprmc_sw_gpu`,
  `setcoef_sw_gpu`, `taumol_sw_gpu`, `reftra_sw_gpu`, `vrtqdr_sw_gpu`,
  `spcvmc_sw_gpu` (6 `_gpu` modules in `module_ra_rrtmg_sw.F`).
- **Not ported, and needed before a chain driver can exist**:
  `mcica_subcol_sw` and SW's `inatm`. There is no
  `mcica_subcol_sw_gpu`, no `inatm_sw_gpu`, no `rrtmg_sw_gpu_chain*`, and
  no `#ifdef WRF_GPU_RAD` branch anywhere in `RRTMG_SWRAD`.
- **The "cloud-optics gap" is not a gap** — same resolution as LW's, and
  for the same reason. `RRTMG_SWRAD` sets `inflgsw = 2` unconditionally
  (bumped to 3/4/5 by `has_reqc`/`has_reqi`/`has_reqs`, never back to 0),
  and its inline cloud optical depth / single-scattering-albedo /
  asymmetry-parameter code is guarded by `if (inflgsw .eq. 0)` — dead for
  every call this project makes. The real cloud optics live in
  `cldprmc_sw`, already ported and verified. **No SW cloud-optics kernel
  needs writing.** (Earlier text here said this had "not even been
  located"; it has, and it costs nothing.)
- **Highest-risk piece to write is SW's `inatm`**, because that is exactly
  where LW's cross-thread `pz(iplon,l-1)` race lived. Write it reading
  `plev` directly, not a neighbour cell of an array the same kernel writes.
  The existing SW kernels were audited for both of today's bug classes and
  are clean: `vrtqdr_sw_gpu` writes every index of its private
  `prup/prupd/ztdn/prdnd` before reading it (its `klev+1` boundary was
  already fixed in an earlier session), `spcvmc_sw_cumprod_gpu`'s
  recurrence is entirely within one thread's own `(iplon,:,ig)` slice, and
  every other SW `private()` clause holds scalars only.
- **Reuse, do not re-derive**: `RRTMG_LWRAD`'s chunk accumulate/flush
  shape, and `rrtmg_lw_gpu_ranks_per_device()` — SW's chunk sizing needs
  the same rank-awareness, so factor that helper out rather than copying
  it.

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
- **`-Mbounds` is silently disabled by NVFORTRAN whenever `-acc` is on.**
  The compiler says so once, quietly:
  `nvfortran-Warning-CUDA Fortran or OpenACC GPU targets disables -Mbounds`.
  Every "bounds checking came back clean" result on this project before
  this was noticed is worthless. To get real bounds checking on a file,
  remove `-acc` from that file's flags (e.g. a per-file `FCFLAGS :=`
  override in the relevant `Makefile`) and rebuild just it.
- **Valgrind cannot run this build's `-O3` binaries** — its VEX JIT does not
  support AVX-512 and the run dies with SIGILL. `-Mnovect` alone is *not*
  enough (AVX-512 still appears in scalar NaN-safe `MAX` codegen, e.g.
  `vcmpunordss` with `%k1` mask registers), and `-tp=skylake` is not enough
  either (skylake-avx512 has AVX-512). `-tp=haswell` works. Also beware
  stale objects in `external/io_netcdf` that a normal rebuild will not
  refresh. And remember valgrind's own allocator masks address-reuse bugs
  exactly the way `compute-sanitizer`'s does — same blind spot, documented
  above.
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

- **When a sequential CPU loop becomes a parallel kernel, audit every array
  read whose index is not the thread's own index.** Two separate real bugs
  in the LW chain (commit `0c1f32692`) were the same mistake in two
  disguises, and both were invisible to code review and to a small
  standalone harness:
  - *Cross-thread read inside one kernel.* `inatm_layers_gpu`'s
    `collapse(2)` kernel wrote `pz(iplon,l)` and read `pz(iplon,l-1)` — a
    value another thread of the same kernel produces. A sequential CPU loop
    gives that ordering for free; a kernel gives no ordering at all. The
    fix is almost always to re-derive the neighbour value from a read-only
    input (here `pz(iplon,l-1) ≡ plev(iplon,l)`) rather than to add
    synchronization. **Rule: inside one kernel, an array that the kernel
    writes may only be read at the writing thread's own index.**
  - *Incompletely initialized thread-private scratch.* `rtrnmc_gpu` zeroed
    only index 0 of `urad`/`drad`/`clrurad`/`clrdrad(0:mxlay)` because the
    CPU original zeroes the whole range *outside* the loop being ported.
    **Rule: for every array moved into a `private()` clause, find where the
    original initialized it and reproduce that initialization inside the
    kernel, for the full index range the kernel reads** — including entries
    the kernel itself never writes. Same family as `vrtqdr_sw_gpu`'s missing
    `prup(klev+1)` and `inatm`'s `pz(iplon,0)`; this keeps recurring and is
    worth a deliberate checklist pass per ported routine.
- **Run-to-run variation in a GPU port's output, on identical inputs, is a
  race or an uninitialized read — never data, never FP reassociation.** FP
  noise is deterministic for a fixed kernel launch geometry. Counting
  affected elements across two runs of the same case (943 bad columns, then
  a *different* 943) is a cheaper and far more decisive test than any
  sanitizer, and it immediately rules out the entire "bad input data" class
  of hypothesis. Note the failing indices may also carry a structural
  signature worth reading: here every surviving bad column after the first
  fix was at a flattened index ≡ 5 (mod 32), i.e. one fixed warp lane.
- **Localize a NaN by instrumenting the pipeline in causal order, in one
  run, with `COUNT(.NOT. finite)` per stage — not with fired-once `SAVE`d
  flags.** Two independently-`SAVE`d one-shot probes fire on *different
  calls*, so comparing them fabricates conclusions; this produced a
  completely wrong root-cause diagnosis that stood for several sessions
  (see the LW status entry, point 8). Counts per stage, printed together at
  one point in time, told the truth in a single run.
- **REAL→INTEGER conversion of NaN/Inf is implementation-defined and CPU
  and GPU codegen are NOT guaranteed to agree on it.** x86's hardware
  float-to-int conversion of NaN yields `INT_MIN`; NVHPC's GPU codegen for
  the same Fortran `int(...)` on a NaN-derived expression does not. A CPU
  routine with an explicit bounds clamp right after such a conversion
  (`jp = int(...); IF (jp.lt.1) jp=1`) can "accidentally" tolerate NaN
  input forever, simply because `INT_MIN` always fails the `.lt. 1` test —
  while the GPU port of the identical arithmetic can produce a value the
  same clamp doesn't catch, and an out-of-range table-lookup index turns
  into an invalid device address at runtime. This bit `taugb1_gpu` for
  real: a pre-existing NaN in the real Hong Kong case's pressure field
  (unrelated to this port) never crashed the CPU path but crashed the GPU
  one with `CUDA_ERROR_ILLEGAL_ADDRESS`/`CUDA_ERROR_INVALID_ADDRESS_SPACE`
  — see the LW driver-integration status entry above for the full
  debugging trail. Takeaway: "the CPU path never crashed on this input"
  is not evidence the input is well-formed — it may just mean the CPU's
  specific (unspecified) NaN-to-int outcome happened to fall inside a
  downstream clamp. When a GPU port's crash is deterministic (same
  simulated time/column every run) rather than address-layout-dependent,
  suspect the *data*, not the port's data-lifecycle plumbing — sanitize
  the primary physical inputs (pressure, temperature) for non-finite/
  non-physical values defensively, with a permanent warning message so
  the underlying data problem stays visible instead of silently
  disappearing.
- **A clean `compute-sanitizer --tool memcheck` run does not rule out a
  device-address-reuse bug.** memcheck uses its own allocator, which does
  not reuse freed host/device addresses the way the default allocator
  does — so a bug that only manifests when a *later* allocation happens to
  land at an address a stale mapping still references can run perfectly
  clean under memcheck while crashing deterministically without it. If a
  crash disappears under `compute-sanitizer` but is otherwise
  reproducible, that is itself informative (points at data lifecycle /
  address reuse), not proof of correctness. `CUDA_LAUNCH_BLOCKING=1` is a
  much cheaper first test for ruling out an async/missing-synchronization
  race specifically (forces every kernel launch to block), without the
  full sanitizer's overhead or its allocator-behavior differences.
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
