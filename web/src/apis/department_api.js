/**
 * 部门管理 API
 */

import { apiDelete, apiGet, apiPost, apiPut } from './base'

const BASE_URL = '/api/departments'

/**
 * 获取当前用户有权读取的组织列表
 * @returns {Promise<Array>} 部门列表
 */
export const getDepartments = () => {
  return apiGet(BASE_URL)
}

/**
 * 获取部门详情
 * @param {number} departmentId - 部门ID
 * @returns {Promise<Object>} 部门详情
 */
export const getDepartment = (departmentId) => {
  return apiGet(`${BASE_URL}/${departmentId}`)
}

/**
 * 创建部门
 * @param {Object} data - 部门数据
 * @param {string} data.name - 部门名称
 * @param {string} [data.description] - 部门描述
 * @returns {Promise<Object>} 创建的部门
 */
export const createDepartment = (data) => {
  return apiPost(BASE_URL, data)
}

/**
 * 更新部门
 * @param {number} departmentId - 部门ID
 * @param {Object} data - 部门数据
 * @param {string} [data.name] - 部门名称
 * @param {string} [data.description] - 部门描述
 * @returns {Promise<Object>} 更新后的部门
 */
export const updateDepartment = (departmentId, data) => {
  return apiPut(`${BASE_URL}/${departmentId}`, data)
}

/**
 * 删除部门
 * @param {number} departmentId - 部门ID
 * @returns {Promise<Object>} 删除结果
 */
export const deleteDepartment = (departmentId) => {
  return apiDelete(`${BASE_URL}/${departmentId}`)
}

export const departmentApi = {
  getDepartments,
  getDepartment,
  createDepartment,
  updateDepartment,
  deleteDepartment
}
