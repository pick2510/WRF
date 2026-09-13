# WRF GPU Porting — Project Knowledge

This file captures accumulated knowledge for the ongoing effort to port WRF's
RRTMG radiation scheme (and, earlier, `dyn_em`) to OpenACC/GPU. Read this
before resuming GPU-port work in this repo.

## Top-level priority

Profiling the real Hong Kong case (`/mnt/nvme/wrf_hk_gpu_d01`, `radt=1`,
`ra_lw_physics=4`, `ra_sw_physics=4` = RRTMG) showed **radiation is ~76% of
total wall-clock time**. Porting RRTMG-LW and RRTMG-SW to GPU was the top
priority, superseding the earlier `dyn_em` OpenACC port (which is done and
stable — see "Prior work" below).

**That work is now complete.** Both RRTMG-LW and RRTMG-SW are fully ported,
chained, integrated into their WRF drivers, and verified against the
CPU-only build at the field level. 10-minute real case: **312.0s CPU 1-rank
→ 103.7s GPU 1-rank (3.0x)**, and the 4-rank GPU build (57.6s) matches the
8-rank CPU build (57.3s). With radiation off the critical path, the next
lever is whatever now dominates — profile before choosing; the scoped-but-
never-started `dyn_em` diffusion port (`module_diffusion_em.F`,
`diff_opt=2`/`km_opt=4`) is the obvious candidate but is no longer
obviously the right one without fresh numbers.

Full architectural analysis, phase plan, and rationale live in `plan.md` at
the repo root — read that for the "why" behind the sequencing. This file is
the "how": conventions, gotchas, and status.

## Which WRF version this branch is (read this first)

Branch `openacc-gpu-v4.7.1` is based on the **v4.7.1** tag. The original port
was written against **V3.9.1.1**, which turned out not to be the version that
produced the user's case inputs — every namelist workaround the 3.9.1.1 base
needed (`dzbot`, `use_wudapt_lcz` and `solar_diagnostics` missing from the
Registry, per-domain `specified`/`nested`/`parent_time_step_ratio`, the
`SF_URBAN_PHYSICS=2`-vs-YSU rejection patched with `ncatted`, and dropping
d03) was a symptom of that mismatch, not a property of the case. On 4.7.1 the
user's `namelist.input` from `/mnt/DC550/HONGKONG/domain_16` runs verbatim,
all three domains, no edits.

The 3.9.1.1 line lives on at `origin/openacc-em-dynamics`, HEAD `d9d434eff`.

### What the forward port cost, and why so little

The `#ifdef WRF_GPU_RAD` additive structure is what made this tractable: the
GPU modules are appended after each CPU module's `end module`, so 3.9.1.1 →
4.7.1 produced **6 merge conflicts across 3 files**, and the final diff
touches only **13 pre-existing lines** in the two 14k-line radiation files
(all but four of them trailing-whitespace strips). Keep it that way.

A clean merge says nothing about correctness here, though: the GPU kernels
are line-by-line transcriptions of the 3.9.1.1 *algorithms*, and they live in
modules neither version of WRF touches, so any CPU routine that changed
between versions leaves its GPU mirror silently stale. `port/audit.py`
extracts each mirrored CPU routine from both revisions and reports how much
it moved. Result:

| CPU routine | changed lines | action |
|---|---|---|
| `inatm`, `cldprmc`, `setcoef`, `taumol`, `cldprmc_sw`, `setcoef_sw`, `vrtqdr_sw` | 0 | none |
| `rtrnmc` | 1 | comment only |
| `inatm_sw` | 2 | loop-order swap, no semantics |
| `taumol_sw` | 4 | **`sfluxzen(:) = 0.0` — re-applied** |
| `reftra_sw` | 4 | **`zwo` guard — re-applied** |
| `spcvmc_sw` | 21 | pure loop fission, no action |
| `mcica_subcol_lw`/`_sw` | 37 / 40 | new `hgt/idcor/juldat/lat` args |
| `generate_stochastic_clouds`/`_sw` | 118 / 110 | new `icld=4,5` exponential overlap |

Run that audit again before trusting any future rebase.

### What is deliberately NOT ported

v4.7.1 adds exponential (`icld=4`) and exponential-random (`icld=5`) cloud
overlap. These are **not** ported. `mcica_subcol_lw_prep_gpu` /
`mcica_subcol_sw_prep_gpu` reject anything outside `icld=1,2,3` with
`wrf_error_fatal` rather than silently computing the wrong overlap. WRF's
`cldovrlp` default is 2 and the user's namelist does not set it, so their
case uses `icld=2`, which is ported and byte-for-byte unchanged from
V3.9.1.1.

### Two v4.7.1 build traps

Both of these fail at *link* time with a message that points at the wrong
thing, and WRF's make ignores the real error (`Error 2 (ignored)`).

1. **`ulimit -s unlimited` is mandatory.** With the default 8 MB stack,
   nvfortran 26.5's `fort2` overflows its own stack and dies with
   `TERMINATED by signal 11` at `-O2` and above on every file carrying WRF
   4.7.1's full `grid` argument list: `frame/module_cpl.F`,
   `frame/module_dm.F`,
   `share/{dfi,mediation_integrate,module_optional_input,wrf_timeseries}.F`,
   `dyn_em/{start_em,nest_init_utils,module_initialize_real}.F`,
   `main/{module_wrf_top,real_em,tc_em}.F`. The only symptom is
   `ld: cannot find ../main/module_wrf_top.o` at link. **This is not the
   `-j` race** WRF has in `main/` — lowering `-j` does not help and wastes
   20 minutes a try (it was tried twice). `-O1` also "fixes" it and is the
   wrong fix.
2. **Delete `-L/usr/lib/x86_64-linux-gnu` from `NETCDF4_DEP_LIB`** in the
   generated `configure.wrf` after every `./configure`. Debian's `nc-config`
   emits it; it precedes the `-L` that NVHPC's `mpif90` wrapper appends, so
   the trailing `-lmpi` binds Debian's OpenMPI instead of HPC-X's and every
   executable fails with
   `libmpi.so: undefined reference to opal_common_ofi_*`. netcdf resolves
   from the linker's default paths without it.

Both are also documented in the `arch/configure.defaults` stanza itself
(menu options 80–83; **82 = dmpar**, which is what this work uses).

## Current status (update this as work progresses)

### v4.7.1 verification against the user's real case — done

Case: `/mnt/DC550/HONGKONG/domain_16`, run on `/mnt/nvme` scratch, the user's
own `namelist.input` unmodified except run length and domain count.
Reference: the **same binary tree with `-DWRF_GPU_RAD -DWRF_GPU_DYN` removed
from `ARCH_LOCAL` and only the three guarded `phys/` files rebuilt**. Same
compiler, same `-O3 -acc -gpu=ccall -Kieee`, so the macro is the only
variable. A separately-configured CPU tree (or the gfortran-built 4.7.1 at
`/home/strebdom/git/WRF`) is a weaker control and was not used.

**Timing, d01 only, 10 model minutes, 4 ranks.** The per-step log separates
radiation cleanly, because `dyn_em` is *not* ported on this line and so the
non-radiation steps are the same code in both binaries:

| | radiation steps | other steps | wall |
|---|---|---|---|
| CPU | ~8.80 s | ~0.95 s | 91 s |
| GPU | ~3.60 s | ~0.96 s | 50 s |

**2.45x on the radiation step, 1.82x end-to-end.** Read the per-step
breakdown, not the median: the median step is a non-radiation step and is
identical in both, so a median-vs-median comparison shows no speedup at all
and is actively misleading.

**Correctness — one radiation call, from a cloudy restart.** Comparing after
a 10-minute free run is worthless here: McICA seeds come from the fractional
part of each column's pressure, so a 1-ulp pressure change re-randomises that
column's subcolumn draw. At 10 minutes the all-sky fields scatter to 11%
pointwise while the domain means still agree to 1.7e-5 and the RMS relative
difference is 0.2% — that is the seed signature, not an error.

A single-step comparison from `wrfinput` is also worthless, for the opposite
reason: **`CLDFRA` is identically zero at 00:00**, clear-sky equals all-sky,
and the McICA path is never entered. The test that actually means something
is: run 10 minutes with `restart_interval=10` to get a cloudy restart, then
restart both builds for exactly one fixed-`dt` step. Results:

- `CLDFRA` bit-identical, max 1.0 — genuinely cloudy, 32% of columns.
- Every LW field and every clear-sky SW field: <= 6e-6 relative.
- All-sky SW: <= 1e-4 relative (0.04 W/m^2 on 404 W/m^2), and **only 10 of
  15625 columns exceed 1e-5**. The other 99.94% sit at the 1e-7/1e-8 float32
  storage floor, the same floor as LW and clear-sky.
- All 10 of those columns are cloudy with several fully-overcast layers.
  Cloudy columns are 32% of the domain, so 10/10 by chance is p ~ 1e-5.
  The residual is confined to optically thick cloud and scales with the
  overcast-layer count.

The mechanism is *consistent with* `reftra_sw`'s `zwo >= zwcrit`
(0.9999995) conservative-scattering branch flipping on ulp-level
host-vs-device differences — water cloud in the visible sits essentially on
that threshold, and `-Kieee` constrains reassociation but not FMA
contraction. That is a discontinuity in RRTMG's own algorithm, where neither
side is more correct than the other. **This is a plausible mechanism, not a
verified one** — it was not proven by instrumenting `zwo`. What *is*
established is the bound and the confinement to cloudy columns.

**Nests — all three domains, natively.** 2 simulated minutes, 4 ranks,
`max_dom=3` (d01 126x126 @6.25km, d02 126x126 @1.25km, d03 121x121 @250m,
`parent_grid_ratio=5,5`). Both builds `SUCCESS COMPLETE WRF`. Per-domain
clear-sky agreement is the thing to read, because it is the part McICA does
not randomise:

| | d01 | d02 | d03 |
|---|---|---|---|
| `SWUPTC` rel. diff of domain mean | 5.4e-9 | 3.4e-9 | 8.4e-9 |
| `SWDNBC` rel. diff of domain mean | 3.5e-9 | 1.1e-8 | 1.1e-8 |
| `T2` rel. diff of domain mean | 2.3e-8 | 5.8e-8 | 2.2e-8 |
| all-sky `SWDOWN` rel. diff of domain mean | 4.5e-5 | 2.1e-5 | 5.9e-5 |

The nests behave exactly like d01, which is what exercises the per-domain
`gpu_lw_first_call(max_domains)` / `gpu_sw_first_call(max_domains)` flags: a
scalar flag would leave d02 and d03 reading output nothing had written, and
that would not come back at 1e-9 on the mean.

Timing, same run: radiation steps on d03 drop from 9.54s to 4.65s (2.05x);
medians (non-radiation steps) are identical at 1.10 vs 1.12s. Wall is only
226s vs 253s (1.12x), and that is expected rather than disappointing — with
`radt=1` and d03's `dt` near 0.88s, d03 takes ~68 steps between radiation
calls, so its 125 steps contain about two radiation calls and the run is
dominated by CPU dynamics that this line does not port. The nested speedup
is bounded by `dyn_em`, not by radiation.

Control run: GPU at 4 ranks vs GPU at 2 ranks, same restart, agrees to
1.4e-6 — so the GPU path is decomposition-stable and those 10 columns are a
real GPU-vs-CPU difference, not chunk-boundary noise. (Note this control
came back *negative*: it was expected to reproduce the outliers via changed
chunk boundaries and did not. Do not reuse it as the McICA perturbation
control; the one that works is described under "Field-level verification"
below.)

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

### RRTMG-SW — fully ported, integrated and verified (module_ra_rrtmg_sw.F)
All behind `#ifdef WRF_GPU_RAD`, all additive. The chain driver, the
`RRTMG_SWRAD` integration and the field-level verification are written up
further down (search "SW is now complete too"); the per-routine list is:
- `mcica_subcol_sw_gpu` (commit `44bc6d712`) — verified bit-exact
- `rrtmg_sw_inatm_gpu` (commit `166d8723c`) — verified bit-exact
- `rrtmg_sw_gpu_chain_driver` (commit `94b77c533`) — verified to ≤4.7e-6 relative
- `RRTMG_SWRAD` accumulate/flush + `rrtmg_sw_gpu_tables` + `rrtmg_sw_gpu_chunk` (commit `89e4eb501`)
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
  Done at 2 and 4 ranks too (see point 9's table). The direct field-level
  comparison of `GLW`/`OLR`/`LWUPB` (and the SW diagnostics) that used to
  be listed here as outstanding is **done** — see "Field-level
  verification" below. The trick was `history_interval = 5`: the standing
  namelist's 30-minute interval means a 10-minute run only ever writes the
  t=0 frame, which predates the first radiation call, so nothing
  radiation-related was ever in the output being compared. Restore
  `history_interval = 30` and `run_minutes = 10` in both
  `/mnt/nvme/wrf_hk_gpu_d01` and `/mnt/nvme/wrf_hk_cpu_d01` afterwards.

**SW is now complete too — ported, chained, integrated into `RRTMG_SWRAD`,
and verified against the CPU build at the field level.** The remaining
pieces landed as:

- `mcica_subcol_sw_gpu` + `mcica_subcol_sw_prep_gpu` +
  `mcica_subcol_sw_driver_gpu` (module `mcica_subcol_gen_sw_gpu`, commit
  `44bc6d712`) — verified **bit-exact** on all 11 outputs across all three
  overlap modes, 3 columns × 45 layers. Bit-exactness is the right bar
  (kissvec is bitwise integer arithmetic), same as the LW twin. Documented
  differences from LW: `ngptsw=112`/`nbndsw=14`, the extra
  `ssac`/`asmc`/`fsfc`, the `ngbm = ngb(1) - 1` band remap (SW's `ngb`
  runs 16..29, not 1..14), and **clear-subcolumn `ssacmcl` defaults to
  `1._rb`, not `0._rb`** — flagged in-code as deliberate, since it reads
  exactly like copy-paste drift from LW and is not.
- `inatm_sw_adjflux` (host) + `inatm_sw_zero_gpu` + `inatm_sw_layers_gpu`
  + `inatm_sw_aerosol_gpu` + `inatm_sw_cloud_gpu` + `inatm_sw_driver_gpu`
  (module `rrtmg_sw_inatm_gpu`, commit `166d8723c`) — verified
  **bit-exact** on all 22 outputs, three cases. Written specifically to
  avoid both LW bugs: `pdp` and `coldry` come from `plev` directly rather
  than from `pz(l-1)` while the same kernel writes `pz(l)`, and
  `inatm_sw_zero_gpu` establishes the original's opening defaults over the
  full index range (not optional: `wkl(5,:)` is never assigned by
  anything, the `iceflag/=5` path never assigns `cswpmc`/`resnmc`, and
  `ssacmc`/`ssaa` default to 1).
  One **deliberate divergence**, documented at the assignment: the
  original sets `inflag`/`iceflag`/`liqflag` only inside `if (icld .ge. 1)`
  yet `rrtmg_sw` passes them to `cldprmc_sw` unconditionally, so the CPU
  reads unassigned `intent(out)` scalars when `icld=0`. Harmless there
  (`icld=0` ⇒ `cldfmc` all zero ⇒ no g-point is acted on), but the port
  assigns them unconditionally. The harness skips the flag comparison when
  `icld < 1` for exactly this reason — a MISMATCH there is the *CPU's*
  undefined value, not the port's.
- `rrtmg_sw_gpu_chain_driver` (module `rrtmg_sw_gpu_chain`, commit
  `94b77c533`) — chains `mcica_subcol_sw_driver_gpu` →
  `inatm_sw_driver_gpu` → `cldprmc_sw_gpu` → `setcoef_sw_driver_gpu` →
  `taumol_sw_driver_gpu` → `spcvmc_sw_driver_gpu`, then reproduces
  `rrtmg_sw`'s own output loop. **Four pieces had to be written that live
  in `rrtmg_sw`'s host code rather than in any named subroutine, and are
  easy to miss when reading the call chain as a list of subroutine calls**:
  `sw_prep_albedo_gpu` (solar-zenith clamp plus the per-band albedo
  expansion — near-IR bands 1-9 and 14 from `aldir`/`aldif`, UV/visible
  10-13 from `asdir`/`asdif`), `sw_transfer_cloud_gpu` (the
  (g-point,layer)→(layer,g-point) transpose; its `icld=0` branch sets
  `zomgcmc` to **1**, not 0), `sw_transfer_aerosol_gpu` (the iaer 0/6/10
  selection, ECMWF six-type sum included so `aer_opt=1` is not silently
  dropped), and `sw_fluxes_gpu` (flux transfer, direct/diffuse split for
  total/UV/near-IR, heating rate from `pdp`). Plus
  `cldprmc_sw_prep_batch_gpu`, the pre-mcica reformulation of the host
  validation — same exactness argument as LW's.
  **There is no SW analogue of `lw_combine_taua_gpu`**: SW never folds
  aerosol into `taug`, it carries `ztaua`/`zasya`/`zomga` into `spcvmc_sw`
  separately, because it needs the scattering properties and not just an
  extinction sum. Also note `taumol_sw` is called from *inside* `spcvmc_sw`
  in the CPU code (unlike `taumol`/`rtrnmc`, which are siblings); the port
  hoisted it out, so in the chain they are two consecutive calls.
  The mcica call is guarded by `icld >= 1`, reproducing the CPU
  `mcica_subcol_sw`'s opening `if (icld.eq.0) return` — **skipping it is
  what reproduces the original, not an optimisation**: running it anyway
  burns the RNG and fabricates subcolumn cloud arrays for a call the CPU
  leaves clear.
  Verified against a CPU chain of the originals on four cases
  (`icld`=1/2/0 and `iaer`=10/6, `iceflag`=3/5): all 12 output fields to
  ≤4.7e-6 relative, primary fluxes at 3-10e-7 — the single-precision floor.
- **`RRTMG_SWRAD` driver integration** (commit `89e4eb501`) — the
  `#ifdef WRF_GPU_RAD` accumulate/flush path, `gpu_flush_sw_chunk`,
  `rrtmg_sw_gpu_tables` (pushed once from `rrtmg_swinit`, *after*
  `rrtmg_sw_ini`, which is what fills the tables — the g-point reduction
  rewrites `absa`/`absb`/`sfluxref` in place), and `rrtmg_sw_gpu_chunk`.
  `rrtmg_lw_gpu_ranks_per_device()` is reused rather than duplicated.
  **One structural difference from LW that is not cosmetic**: SW skips
  every column with the sun below the horizon, so the chunk is a subset of
  the tile and LW's "flush when `i==ite .and. j==jte`" trigger is
  unusable — the tile's last column may be dark and never accumulate.
  Flush when full, plus once after both loops for the remainder (which is
  legitimately zero for an entirely dark tile).
- SW costs **~5x LW per column** (~1.7MB at nlay=65), almost entirely
  because `spcvmc_sw`'s adding-doubling solver keeps far more
  `ngptsw`-wide state resident (32 arrays inside `spcvmc_sw_driver_gpu`
  alone) than `rtrnmc`'s up/down sweep does.
- **The "cloud-optics gap" is not a gap** — same resolution as LW's, and
  for the same reason. `RRTMG_SWRAD` sets `inflgsw = 2` unconditionally
  (bumped to 3/4/5 by `has_reqc`/`has_reqi`/`has_reqs`, never back to 0),
  and its inline cloud optical depth / single-scattering-albedo /
  asymmetry-parameter code is guarded by `if (inflgsw .eq. 0)` — dead for
  every call this project makes. The real cloud optics live in
  `cldprmc_sw`, already ported and verified. **No SW cloud-optics kernel
  needs writing.**

**Wall clock with SW on the GPU too** (real Hong Kong case, 10 minutes
simulated, same machine, `nvfortran -O3`), `SUCCESS COMPLETE WRF` and zero
"non-physical p/T" warnings at every rank count:

| build | ranks | wall |
|---|---|---|
| CPU-only | 1 | 312.0s |
| GPU dyn + LW only | 1 | 245.1s |
| GPU dyn + LW + **SW** | 1 | **103.7s** |
| GPU dyn + LW + SW | 2 | 69.5s |
| GPU dyn + LW + SW | 4 | **57.6s** |
| CPU-only | 4 | 100.6s |
| CPU-only | 8 | 57.3s |

3.0x over the 1-rank CPU build, and the 4-rank GPU build now *matches the
8-rank CPU build* — the first time this port has reached the best CPU
number rather than merely beating a like-for-like rank count. `dmudt`
agrees with the CPU-only baseline to 5 significant figures at 1, 2 and 4
ranks (94.76674 / 94.76756 / 94.76801 vs 94.7676).

### Field-level verification — done, and the McICA control is the point
This closes the item that had been outstanding for LW as well. Run both
builds with `history_interval = 5` (the standing 30-minute setting only
ever writes t=0, which predates the first radiation call), then compare
`SWDOWN`/`SWUPB`/`SWUPT`/`SWDNBC`/`GLW`/`OLR`/`LWUPB` directly.

Cloud-free fields agree at the single-precision floor — clear-sky
`SWDNBC` to 6e-3 W/m2 out of 404 pointwise, 2e-9 relative in the domain
mean. **But cloud-affected fields show max pointwise gaps of tens of
percent** (126.7 W/m2 on `SWDOWN`), and that needs explaining rather than
excusing.

It is McICA, and the control proves it rather than asserting it. McICA
seeds its RNG from the *fractional part* of the column's own pressure
(`seed1 = (pmid1 - int(pmid1)) * 1e9`), so a **single-ulp pressure change
re-randomizes that column's subcolumn draw completely** — there is no
"small perturbation" regime. Comparing the **GPU build at 1 rank against
the GPU build at 2 ranks** — identical code, identical build, perturbed
only by domain decomposition, which cannot be a porting bug — reproduces
the *same* scatter:

| | max &#124;ΔSWDOWN&#124; | cols >10 W/m2 | mean &#124;Δ&#124; | signed mean Δ |
|---|---|---|---|---|
| CPU 1-rank vs GPU 1-rank | 126.696 | 22 (0.14%) | 0.120 | +0.009 |
| GPU 1-rank vs GPU 2-rank | 126.696 | 22 (0.14%) | 0.113 | −0.020 |

Identical maxima (the same bimodal column flipping in both), the same
count of affected columns, and a signed mean bias of opposite sign in the
two cases — i.e. noise, with no systematic offset. Domain means agree to
2.4e-5 relative or better on every field, LW included.

**Methodological note worth keeping**: the *first* control tried was CPU
1-rank vs CPU 2-rank, and it came back **bit-identical** — so it tested
nothing at all. A control only works if it actually applies the
perturbation whose effect you are trying to attribute; check that it does
before reading anything into it. WRF's dynamics are bit-reproducible
across decompositions in this configuration, which is exactly why that
control was inert and the GPU-vs-GPU one was not.

### Nests, OpenMP, memory limits and device portability (commit `881318096`)

**Nests work, and this is measured, not reasoned.** Domains are integrated
sequentially (`frame/module_integrate.F`'s `RECURSIVE integrate`), so two
domains never call radiation concurrently within a rank; all batch state is
allocated and freed inside one `RRTMG_*RAD` call; chunk sizing is
recomputed per call from that domain's own tile, so a nest gets its own
chunking (including under vertical nesting, since `nlayers`/`gpu_sw_nlay`
are re-derived per call); the table push is guarded by a `SAVE`d flag and
so happens once across all domains.

One real bug was found and fixed: `gpu_lw_first_call`/`gpu_sw_first_call`
were `SAVE`d **scalars** shared by every domain, so domain 1's first call
consumed the flag and a nest's genuine first call was treated as "not
first" — holding an output value nothing had ever written. They are now
arrays keyed by a new `OPTIONAL` `gpu_domain_id` argument, forwarded from
`module_radiation_driver` (the only change that file has ever needed).
`OPTIONAL` so no other caller is affected.

Verified on the real two-domain case (d01 126×126 @6.25km, d02 126×126
@1.25km, `parent_grid_ratio=5`), 2 simulated minutes, 4 ranks, 5 radiation
calls on d01 and 25 on d02, zero warnings. Against the CPU-only build,
**`dmudt` agrees to 6 significant figures on both domains** (d01 167.4833
vs 167.4818; d02 359.9202 vs 359.9213). GPU 60.2s vs CPU 77.8s — nests are
1.29x faster, less than the single-domain 1.8x because d02 runs 5x more
steps and the dynamics are at parity there.

**The four namelist fixes below are V3.9.1.1-only. Do not apply them on
this branch.** They were symptoms of running a 4.7.1 case on a 3.9.1.1
binary, and each was worked around individually instead of prompting the
obvious question of which WRF built the inputs. On v4.7.1 the case runs
verbatim, d03 included. Kept here only so the same symptoms are recognised
as a version mismatch next time rather than re-debugged as nesting errors:
- `dzbot` is not in this version's Registry. Its presence makes
  `READ(NML=domains)` fail *silently* in `external/RSL_LITE/module_dm.F`'s
  sneak-peek, leaving `parent_id` unset — the symptom is
  "invalid parent id for domain 2", which looks like a nesting error and
  is not. Same class: `use_wudapt_lcz` (`&physics`) and
  `solar_diagnostics` (`&diags`).
- `specified`/`nested` need one entry per domain
  (`.true.,.false.` / `.false.,.true.`); a single entry leaves nests on
  Registry defaults and fails boundary-condition validation.
- `parent_time_step_ratio` likewise needs `1,5,5`.
- The `wrfinput_d0*` files carry `SF_URBAN_PHYSICS = 2`, which this version
  only allows with MYJ or BouLac PBL while the case uses YSU — and PBL must
  match across domains. For a radiation test, patch the attribute on local
  copies (`ncatted -a SF_URBAN_PHYSICS,global,o,l,0`) rather than changing
  the PBL scheme. d03 was left out for the same reason.
Scratch dirs used: `/mnt/nvme/wrf_hk_nest_gpu`, `/mnt/nvme/wrf_hk_nest_cpu`
(inputs symlinked from `/mnt/DC550`, outputs on nvme).

**Benchmarking trap, nearly reported as a regression**: the first nested
comparison showed GPU 137.5s vs CPU 77.9s and looked like a serious GPU
slowdown on nests. It was an artifact — the GPU run went first with a cold
page cache (9.5GB `wrffdda_d01` on spinning disk) and a cold CUDA code
cache, and the CPU run then benefited from both being warm. Re-run warm and
alternating, it is GPU 60.2/60.3s vs CPU 77.8/78.4s, stable. **Never
compare two builds on their first run over cold input; alternate the order
and discard the first pair.**

**OpenMP tiling is NOT safe, and is now guarded rather than fixed.**
`module_radiation_driver` calls both `RRTMG_LWRAD` and `RRTMG_SWRAD` from
inside an `!$OMP PARALLEL DO` over tiles (the region spans lines 968–2272),
and this port's batch state (`gpu_nfill`, `gpu_play`, `gpu_sw_*`, ~40
arrays per band) is **module-level and therefore shared between threads**.
It is safe today only because `configure.wrf` has `OMP = # -mp -Mrecursive`,
which forces `num_tiles=1`; serial multi-tile (`numtiles>1`, OpenMP off) is
fine since each call completes before the next. An `#ifdef _OPENMP` runtime
check now aborts with an explanatory message instead of silently racing.
Two things to know before fixing it properly: the LW state was originally
**local** to `RRTMG_LWRAD` (reached by host association from the nested
flush routine, which is thread-safe) and was moved to module scope during a
debugging session that did not need it — so the fix is largely a revert;
and per-thread state alone is not enough, because N threads each sizing a
chunk from the same whole-device free-memory query would over-commit the
GPU, so the chunk must be divided by thread count as well as rank count.
(Note upstream WRF already has `integer, save :: nlayers` in this module
from the Cavallo boundary-condition commit, so the file was not strictly
tile-safe before either — but that is one scalar with the same value on
every tile, which is benign; this is not.)

**Memory: a too-big domain is not a failure mode.** The chunk is capped at
the tile and floored at 1, so a larger domain simply means more chunks.
Measured (`/tmp/rrtmg_integrate/check_chunksize_sw.f90`, which prints this
for the running device and for a range of hypothetical ones):

| device | SW cols/chunk, 1 rank | 4 ranks | 8 ranks |
|---|---|---|---|
| 2GB | 526 (31 chunks) | 131 | 65 |
| 8GB (this card) | 2104 (8) | 526 | 263 |
| 24GB | 6312 (3) | 1578 | 789 |
| 80GB | 15876 (**1**) | 5260 | 2630 |

Two gaps were closed:
- **Host memory scales with the chunk too, and nothing accounted for it.**
  The chain drivers `ALLOCATE` a host mirror of every device intermediate,
  because `!$acc enter data create(x)` needs `x` to exist on the host. A
  2318-column SW chunk is **~4GB of host RAM per rank**; a large-VRAM card
  would otherwise pick a chunk the node cannot allocate. Chunk sizing now
  also reads `/proc/meminfo` `MemAvailable` and bounds by it, divided by
  ranks **per node** (`rrtmg_lw_gpu_ranks_per_node`, filled by the existing
  communicator split) rather than ranks per GPU — host memory is shared by
  every rank on the box however the GPUs are divided between them.
- Warn instead of silently degrading when the free-memory query returns 0
  or the chunk clamps to 1 column. Neither is incorrect (the pipeline works
  at any chunk size) but both mean the run will be very slow.

Still absent, deliberately: there is no `stat=` on the ~174 GPU-path
`ALLOCATE`s, and **device** OOM from `!$acc enter data` is not catchable in
OpenACC at all. The mitigation is the sizing above, not error handling.

**Device portability.** Runtime sizing was already device-independent
(`acc_get_property` + a measured ranks-per-device count; verified from 2GB
to 80GB). The *build* was not: `-gpu=cc120` meant the executable only
loaded on Blackwell. Now **`-gpu=ccall`** in both `configure.wrf` and the
tracked `arch/configure_new.defaults`, so one binary carries device code
for **sm_75 through sm_121** — 12 architectures, Turing through Blackwell
(`cuobjdump --list-elf main/wrf.exe` to check). Costs binary size (114MB)
and compile time; measured runtime cost is nil (single domain, 4 ranks:
57.6s → 55.6s, inside run-to-run noise).

Two device assumptions remain, documented rather than removed:
- `acc_device_nvidia` is hardcoded in 5 places — NVIDIA-only.
- `safety_fraction = 0.5` must cover the CUDA context, the tables, and the
  kernel local-memory reservation. That last term scales with **max
  resident threads (≈ SM count)**, which is *not* proportional to VRAM — so
  this is the one number implicitly tuned to a class of device. On a
  many-SM/modest-VRAM part it may be too generous; on an 80GB card it
  strands memory. OpenACC exposes no SM count, so raising this properly
  needs either a CUDA-driver query or an env-var override.

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
  The same wrong-PATH failure also bites one-off test compiles: a manual
  `mpif90` invocation with only the `compilers/bin` half of the PATH still
  falls through to gfortran, because `mpif90` itself lives under
  `comm_libs/mpi/bin`. Both halves, always.
- **`ulimit -s unlimited` gotcha**: see "Two v4.7.1 build traps" above.
  Unlike the PATH problem this one produces a *plausible-looking* error —
  `ld: cannot find ../main/module_wrf_top.o` — that reads like WRF's known
  `-j` race in `main/`. It is not. Check the stack limit first.
- **A file can silently lose `-acc` and still build, run, and produce
  correct answers — just on the CPU.** `phys/Makefile`'s
  `ifneq ($(GPU_RAD_FLAGS),)` stanza names specific objects. For a long
  time `module_ra_rrtmg_sw.o` was *not* named, yet every full build
  compiled it with `-acc` anyway: GNU make target-specific variables
  propagate to **prerequisites**, and `module_radiation_driver.o` (which
  *is* named) depends on it via `main/depend.common`, so it inherited the
  flags from that rule. `make module_ra_rrtmg_sw.o` on its own dropped
  them, with no error and no warning — the `!$acc` directives simply
  become comments and the "GPU" kernels run as ordinary host loops. Fixed
  by naming the object explicitly (commit `89e4eb501`). **Two lessons:**
  a `_gpu` routine producing correct results is not evidence it ran on the
  GPU, and if you want to know, `NVCOMPILER_ACC_NOTIFY=1` prints one line
  per real kernel launch (`check_sw_chain` shows 237, covering every
  stage). Check that before concluding anything from a verification run,
  and check `grep -- -acc` on the file's compile line in the build log
  whenever a rebuild was targeted rather than full.
- **Never benchmark two builds on their first run over cold input.** The
  first nested-case comparison showed the GPU build at 137.5s against the
  CPU build's 77.9s and looked like a real slowdown; it was entirely the
  GPU run going first with a cold page cache (a 9.5GB `wrffdda_d01` on
  spinning disk) and a cold CUDA code cache, with the CPU run then getting
  both warm. Warm and alternating, it is 60.2s vs 77.8s. Alternate the
  order, run each at least twice, and discard the first pair.
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
   then `export J="-j 12"; ./compile em_real` in the background (with a
   Monitor loop watching `/proc/meminfo` and grepping the log for
   `Executables successfully built`). Fix any syntax errors before
   proceeding.
   **Every build must start with these three lines**, in this order:
   ```
   export PATH=/mnt/nvme/hpc_sdk/Linux_x86_64/26.5/compilers/bin:/mnt/nvme/hpc_sdk/Linux_x86_64/26.5/comm_libs/mpi/bin:$PATH
   ulimit -s unlimited
   export J="-j 12"
   ```
   `ulimit -s unlimited` is not optional on the v4.7.1 line — see
   "Two v4.7.1 build traps" below. Both traps surface only at link time,
   and neither names the real cause.
   **Always build in parallel** — this is a 40-core box with 62GB RAM, and
   `-j 1` wastes most of it. `-j 12` is a good default: WRF's own build is
   partly serialised by module dependencies, so higher `-j` buys little,
   and it leaves headroom against the memory-exhaustion failure described
   under "Memory" below (nvfortran with `-acc -gpu=ccall` is heavier per
   process than a plain CPU compile).
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
  - `gpu_tables_sw_aer_todevice()` — `rrsw_aer`'s `rsrtaua`/`rsrpiza`/
    `rsrasya`, the ECMWF six-type band ratios. Read only by
    `sw_transfer_aerosol_gpu`'s `iaer=6` branch (WRF `aer_opt=1`), which
    the Hong Kong case does not exercise — ported anyway so the GPU path
    is not a silent capability regression against the CPU one.
  - **The production copies now live in the source**, not only here:
    `rrtmg_lw_gpu_tables::gpu_tables_lw_todevice` (called from
    `rrtmg_lwinit`) and `rrtmg_sw_gpu_tables::gpu_tables_sw_todevice`
    (called from `rrtmg_swinit`, and it must come **after**
    `rrtmg_sw_ini` — that is what fills the tables, since the g-point
    reduction rewrites `absa`/`absb`/`sfluxref` in place). The
    `/tmp/rrtmg_integrate/gpu_tables.f90` versions remain for the
    standalone harnesses; keep the two in sync when adding a table.
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
  `check_taumol_sw.f90`, `check_spcvmc_sw.f90`, `check_mcica_sw.f90`,
  `check_inatm_sw.f90`, `check_sw_chain.f90`). Use these as templates for
  the next stage's harness.
  **Harnesses must link with `mpifort`, not `nvfortran`**, since
  `rrtmg_lw_gpu_ranks_per_device` calls MPI; `dm_stubs.f90` supplies a
  one-line `wrf_get_dm_communicator` returning `MPI_UNDEFINED`. (The source
  deliberately reaches the communicator through that accessor rather than
  `USE module_dm`, precisely so a harness can stub it instead of dragging
  in the whole RSL_LITE/DM layer.)
  `check_sw_chain.f90` is worth reading for **how to verify a quantity that
  is a finite difference of two nearly-equal large numbers**. SW's heating
  rate is `(net flux above − net flux below) * heatfac/pdp`, so it
  multiplies flux noise by `heatfac/pdp` — order 10-30 in thin upper
  layers — and lands at ~1e-4 relative while the fluxes themselves sit at
  1e-6. Rather than loosen the tolerance and hope, the harness recomputes
  `swhr` on the host from **each side's own returned fluxes** and checks
  each side against its own recomputation. Both come out bit-exact, which
  separately pins "is this the same formula `rrtmg_sw` uses" and "does
  `sw_fluxes_gpu` implement that formula", and leaves the residual fully
  explained by the already-verified flux agreement. Reuse this whenever a
  tolerance argument starts to feel like special pleading.
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

- The 4.7.1 work lives on the named branch `openacc-gpu-v4.7.1`, based on
  the `v4.7.1` tag. (On the older 3.9.1.1 line a detached HEAD was expected
  and normal — `* (HEAD detached from V3.9.1.1)` — and was not an error to
  fix. That line is now `origin/openacc-em-dynamics`, HEAD `d9d434eff`.)
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
