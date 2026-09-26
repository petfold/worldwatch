# Research agenda — heavy tails and dependence between sources

Status: **parked for research** (2026-09-26). Nothing here is implemented; the
current engine uses the naive rules in `doc/adr/0001-per-source-alert-policy.md`.
Written down after discussion with the operator so the reasoning isn't lost.

## Why this matters

The target is Taleb's black swans: rare, extreme, joint departures. Two things
decide whether the system sees them correctly:

1. **Each stream's own tail** — how rare a value really is under that stream's
   normal behaviour.
2. **Dependence between streams in the tails** — whether several sources going
   extreme together are several confirmations or one signal counted several
   times (two detectors in one building, news outlets reprinting the same wire
   copy, a syndicated chain sharing articles).

## What's wrong with the obvious tools

- **Covariance / correlation** is dominated by the bulk, not the tails.
- Probit-transforming calibrated q-values (z = Φ⁻¹(q)) and using their
  correlation matrix amounts to a **Gaussian copula**. For any correlation
  below 1 it has **zero tail dependence**: the further out you go, the more
  independent streams look. That is backwards for joint extremes; copying
  sources would be counted as nearly independent confirmations.
- Mutual information is marginal-free, but the familiar log-det formula is
  Gaussian, and MI in general is dominated by the bulk.

## Candidate methods

### Dependence in the extremes

- **Tail-dependence coefficient χ(u) = P(B extreme | A extreme)** as u → 1, and
  **χ̄** (Coles, Heffernan & Tawn 1999) to separate asymptotic dependence from
  asymptotic independence. Rank-based: uses only calibrated q-values.
- **Extremal coefficient θ** ∈ [1, d]: the effective number of independent
  sources in the extremes (1 = fully shared, d = independent). Estimated
  nonparametrically via the **madogram** (Cooley, Naveau & Poncet 2006).
  → "internal confirmation" becomes *θ of the agreeing sensors ≥ k*, not a
  raw sensor count.
- **Graphical models for extremes** (Engelke & Hitz 2020; Hüsler–Reiss
  models; structure learning "EGlearn", Engelke, Lalancette & Volgushev 2022):
  the extreme-value counterpart of the spec's sparse Gaussian graphical model.
  The sparsity pattern of the extremal variogram's precision-like matrix *is*
  the coupling graph, learned from joint extremes. Candidate Layer-1 model.
- Online, local alternatives in the spirit of anti-Hebbian decorrelating
  networks (Földiák 1990) — relevant to the constant-cost-per-observation
  constraint; whitening is the counting operation, sparse coding the route to
  "which groups of streams act as one cause" (fingerprints).

### Combining evidence under unknown heavy-tailed dependence

- **Cauchy combination test** (Liu & Xie 2020) and **harmonic mean p-value**
  (Wilson 2019): valid for small p under arbitrary dependence, dominated by the
  most extreme inputs. Safe default before dependence is learned; a learned θ
  or extremal graph gives the efficient version.

### Each stream's own tail

- Count flavor's negative-binomial tail decays geometrically — thin even at the
  burstiest dispersion; genuine cascades would be over-scored.
- Continuous flavor's Student-t tail is power-law (better).
- Planned: **generalised Pareto (peaks-over-threshold) tail** on Layer-0
  predictives, filling the existing `tail_index` column.
- **Tail calibration tests**: bulk KS says little about 1-in-10⁴; check tail
  coverage at 10⁻³…10⁻⁴, pooled across similar streams (hierarchical).
- Beyond the model's data, rarity is extrapolation: display caps at "over
  1-in-1M"; read it as "far beyond anything seen", not a precise probability.

### Scarce data

Thousands of stations, weeks of hourly data: dependence must be learned with
priors — distance decay (co-located ≈ fully dependent), shrinkage, sparsity.

## Proposed order (when research resumes)

1. Internal confirmation via θ among agreeing stations (madogram + distance prior).
2. Cross-source corroboration via the Cauchy combination test (ADR amending §8).
3. GPD tails on Layer-0 predictives + tail-calibration tests.
4. Layer 1 as a graphical model for extremes (spec amendment).

Related, not tail-specific: news outlets as separate series with learned
co-publication (syndication) structure — same machinery, parked with GDELT's
move to context-only.
