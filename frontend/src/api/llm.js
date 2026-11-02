import service from './index'

/**
 * LLM 连接状态：profiles（不含密钥）、各阶段路由、agent pool、ChatGPT 登录状态
 * @param {Boolean} [refresh] - true 时重新检查 Claude Code CLI 状态（跳过 30 秒缓存）
 * @returns {Promise} data: {profiles, routes, agent_pool, default_profile_id, oauth: {chatgpt}, cli: {profile_id: {installed, version, signed_in}}}
 */
export function getLlmStatus(refresh = false) {
  return service({
    url: '/api/llm/status',
    method: 'get',
    params: refresh ? { refresh: 1 } : undefined
  })
}

/**
 * Token 用量（按 stage / profile / model 汇总）
 * @returns {Promise} data: {entries: [...], totals: {...}}
 */
export function getLlmUsage() {
  return service({
    url: '/api/llm/usage',
    method: 'get'
  })
}

/**
 * 开始 "Continue with ChatGPT" 登录
 * @param {Boolean} newAccount - true 时强制注册新账号
 * @returns {Promise} data: {authorize_url, attempt_id}
 */
export function startChatgptSignIn(newAccount = false) {
  return service({
    url: '/api/llm/oauth/chatgpt/start',
    method: 'post',
    data: { new_account: Boolean(newAccount) }
  })
}

/**
 * 查询 ChatGPT 登录状态（不含 token）
 * @param {String} [attemptId] - start 返回的 attempt_id
 * @returns {Promise} data: {signed_in, email, models, expires_at, plan_usage_enabled, attempt, ...}
 */
export function getChatgptStatus(attemptId) {
  return service({
    url: '/api/llm/oauth/chatgpt/status',
    method: 'get',
    params: attemptId ? { attempt_id: attemptId } : undefined
  })
}

/**
 * 退出 ChatGPT 登录（撤销并清除 token）
 * @returns {Promise} data: {signed_out, revoked}
 */
export function signOutChatgpt() {
  return service({
    url: '/api/llm/oauth/chatgpt/signout',
    method: 'post'
  })
}
