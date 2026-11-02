// Run plan chosen on Home ({ mode: 'quick' | 'full', auto: boolean, done: {...} }).
// Kept in localStorage so it survives the route changes between steps and a reload.
// Keyed by project id until a simulation exists, then by simulation id.
const STORAGE_KEY = 'mirofish.runPlans'
const MAX_PLANS = 20

export const QUICK_TEST_ROUNDS = 10

const readAll = () => {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}') || {}
  } catch {
    return {}
  }
}

const writeAll = (plans) => {
  const ids = Object.keys(plans)
  for (const id of ids.slice(0, Math.max(0, ids.length - MAX_PLANS))) delete plans[id]
  localStorage.setItem(STORAGE_KEY, JSON.stringify(plans))
}

export const getRunPlan = (id) => (id ? readAll()[id] || null : null)

export const setRunPlan = (id, plan) => {
  if (!id || !plan) return
  const plans = readAll()
  plans[id] = { done: {}, ...plan }
  writeAll(plans)
}

export const clearRunPlan = (id) => {
  const plans = readAll()
  delete plans[id]
  writeAll(plans)
}

// Returns true once per (id, step): the caller performs the automatic action only then,
// so revisiting a page never repeats a paid step.
export const claimAutoStep = (id, step) => {
  const plans = readAll()
  const plan = plans[id]
  if (!plan?.auto || plan.done?.[step]) return false
  plan.done = { ...(plan.done || {}), [step]: true }
  writeAll(plans)
  return true
}
