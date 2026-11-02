import service from './index'

// 304 Not Modified is a valid answer to a conditional graph request
const acceptNotModified = (status) => (status >= 200 && status < 300) || status === 304

// The response interceptor hands callers `response.data` only, so the ETag
// and the 304 outcome are folded into the envelope here. 304 responses carry
// no body and resolve to `{ success: true, notModified: true, etag }`.
const withGraphMeta = (data, headers, status) => {
  const etag = (typeof headers?.get === 'function' ? headers.get('etag') : headers?.etag) || null
  if (status === 304) {
    return { success: true, notModified: true, etag }
  }
  if (data && typeof data === 'object' && !Array.isArray(data)) {
    return { ...data, notModified: false, etag }
  }
  return data
}

/**
 * 生成本体（上传文档和模拟需求）
 * @param {Object} data - 包含files, simulation_requirement, project_name等
 * @returns {Promise}
 */
export function generateOntology(formData) {
  return service({
    url: '/api/graph/ontology/generate',
    method: 'post',
    data: formData,
    headers: {
      'Content-Type': 'multipart/form-data'
    }
  })
}

/**
 * 构建图谱
 * @param {Object} data - 包含project_id, graph_name等
 * @returns {Promise}
 */
export function buildGraph(data) {
  return service({
    url: '/api/graph/build',
    method: 'post',
    data
  })
}

/**
 * 查询任务状态
 * @param {String} taskId - 任务ID
 * @returns {Promise}
 */
export function getTaskStatus(taskId) {
  return service({
    url: `/api/graph/task/${taskId}`,
    method: 'get'
  })
}

// Last graph payload together with its ETag. The next request for the same
// graph is conditional (If-None-Match); a 304 resolves with the held payload,
// so callers keep assigning `res.data` (same object => no reactive change,
// no redraw) and can read `res.notModified` to skip logging or updates.
let lastGraph = null

/**
 * 获取图谱数据
 * @param {String} graphId - 图谱ID
 * @returns {Promise} envelope `{ success, data, notModified, etag }`
 */
export function getGraphData(graphId) {
  const held = lastGraph && lastGraph.graphId === graphId ? lastGraph : null
  const config = {
    url: `/api/graph/data/${graphId}`,
    method: 'get',
    validateStatus: acceptNotModified,
    transformResponse: [].concat(service.defaults.transformResponse, withGraphMeta)
  }
  if (held) {
    config.headers = { 'If-None-Match': held.etag }
  }
  return service(config).then((res) => {
    if (res.notModified) {
      return { ...res, data: held.data }
    }
    if (res.success) {
      lastGraph = res.etag ? { graphId, etag: res.etag, data: res.data } : null
    }
    return res
  })
}

/**
 * 获取项目信息
 * @param {String} projectId - 项目ID
 * @returns {Promise}
 */
export function getProject(projectId) {
  return service({
    url: `/api/graph/project/${projectId}`,
    method: 'get'
  })
}
