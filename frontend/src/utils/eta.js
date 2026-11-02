// Remaining-time estimate from progress samples [{ t: ms, done: number }].
// Uses the progress rate over the recent window so a slow start (model warm-up,
// ontology) does not dominate the estimate once work speeds up.
const WINDOW_MS = 3 * 60 * 1000
// Below this much observed progress the rate is mostly noise, so show 'estimating'.
const MIN_OBSERVED_MS = 20 * 1000

export const estimateRemainingMs = (samples, total, startedAtMs, nowMs) => {
  if (!total || !samples.length) return null
  const last = samples[samples.length - 1]
  if (last.done >= total) return 0
  if (last.done <= 0) return null

  const first = samples.find(s => s.done > 0) || last
  if (last.t - first.t < MIN_OBSERVED_MS && samples.filter(s => s.done > 0).length < 3) return null

  const recent = samples.filter(s => nowMs - s.t <= WINDOW_MS && s.done < last.done)
  const base = recent.length ? recent[0] : null
  let rate = null
  if (base && last.t > base.t) {
    rate = (last.done - base.done) / (last.t - base.t)
  } else if (startedAtMs && last.t > startedAtMs) {
    rate = last.done / (last.t - startedAtMs)
  }
  if (!rate || rate <= 0) return null

  const sinceLast = Math.max(0, nowMs - last.t)
  return Math.max(0, (total - last.done) / rate - sinceLast)
}

export const splitDuration = (ms) => {
  const totalSeconds = Math.max(0, Math.round(ms / 1000))
  return {
    h: Math.floor(totalSeconds / 3600),
    m: Math.floor((totalSeconds % 3600) / 60),
    s: totalSeconds % 60,
  }
}
