/**
 * 临时存储待上传的文件和需求
 * 用于首页点击启动引擎后立即跳转，在Process页面再进行API调用
 */
import { reactive } from 'vue'

const state = reactive({
  files: [],
  simulationRequirement: '',
  projectBrief: {},
  webEvidence: {},
  isPending: false
})

export function setPendingUpload(files, requirement, projectBrief = {}, webEvidence = {}) {
  state.files = files
  state.simulationRequirement = requirement
  state.projectBrief = projectBrief
  state.webEvidence = webEvidence
  state.isPending = true
}

export function getPendingUpload() {
  return {
    files: state.files,
    simulationRequirement: state.simulationRequirement,
    projectBrief: state.projectBrief,
    webEvidence: state.webEvidence,
    isPending: state.isPending
  }
}

export function clearPendingUpload() {
  state.files = []
  state.simulationRequirement = ''
  state.projectBrief = {}
  state.webEvidence = {}
  state.isPending = false
}

export default state
