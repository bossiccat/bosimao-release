'use strict';

// ADR-027 sidecar package 公共入口。
// 拆分为单向依赖：sidecar-package-common（constants/schema/provenance/resolve 基础层）
// → sidecar-package-verify（verifyPackage 上层）。本文件承载 buildPackage 编排，
// 并再导出公共 API，保持既有 require('./lib/sidecar-package') 引用不变。

const { buildPackage: buildSidecarPackage } = require('./sidecar-package-build');
const common = require('./sidecar-package-common');
const { verifyPackage } = require('./sidecar-package-verify');
const {
  createCurrentPointer,
  createRuntimeLayout,
  finalizeStagedGeneration,
  generationIdForProvenance,
  publishCurrentPointer,
} = require('./sidecar-runtime-publish');

function buildPackage(config) {
  return buildSidecarPackage(config, {
    APP_SOURCES: common.APP_SOURCES,
    createProvenance: common.createProvenance,
    fail: common.fail,
    sha256File: common.sha256File,
    verifyAppSourceSet: common.verifyAppSourceSet,
    verifyPackage,
    createCurrentPointer,
    createRuntimeLayout,
    finalizeStagedGeneration,
    generationIdForProvenance,
    publishCurrentPointer,
    closedFileMap: common.closedFileMap,
  });
}

module.exports = {
  APP_SOURCES: common.APP_SOURCES,
  GENERATION_METADATA_FILE: common.GENERATION_METADATA_FILE,
  HASH_RE: common.HASH_RE,
  INSTALLED_BIN: common.INSTALLED_BIN,
  PROVENANCE_DIGEST_FILE: common.PROVENANCE_DIGEST_FILE,
  PROVENANCE_FILE: common.PROVENANCE_FILE,
  SCRIPT_VERSION: common.SCRIPT_VERSION,
  SHA_FILE: common.SHA_FILE,
  TARGET_TRIPLE: common.TARGET_TRIPLE,
  PackageError: common.PackageError,
  buildPackage,
  closedFileMap: common.closedFileMap,
  createProvenance: common.createProvenance,
  expectedBundleResourceMap: common.expectedBundleResourceMap,
  resolveCurrentGeneration: common.resolveCurrentGeneration,
  sha256File: common.sha256File,
  verifyPackage,
};
