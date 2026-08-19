'use strict';

// ADR-027 sidecar runtime publisher 公共入口。
// 拆分为单向依赖：sidecar-runtime-protocol（schema/layout/finalize/pointer 基础层）
// → sidecar-runtime-lease-gc（lock/lease/GC 上层）。本文件仅做公共 API 再导出，
// 保持既有 require('../lib/sidecar-runtime-publish') 引用不变。

const protocol = require('./sidecar-runtime-protocol');
const leaseGc = require('./sidecar-runtime-lease-gc');

module.exports = {
  assertStableRoot: protocol.assertStableRoot,
  createCurrentPointer: protocol.createCurrentPointer,
  createGenerationMetadata: protocol.createGenerationMetadata,
  createRuntimeLayout: protocol.createRuntimeLayout,
  generationIdForProvenance: protocol.generationIdForProvenance,
  parseCurrentPointer: protocol.parseCurrentPointer,
  parseGenerationId: protocol.parseGenerationId,
  publishCurrentPointer: protocol.publishCurrentPointer,
  publishRuntime: protocol.publishRuntime,
  replaceCurrentPointer: protocol.replaceCurrentPointer,
  finalizeStagedGeneration: protocol.finalizeStagedGeneration,
  verifyFinalizedGeneration: protocol.verifyFinalizedGeneration,
  acquireGenerationLease: leaseGc.acquireGenerationLease,
  acquireReaderLease: leaseGc.acquireReaderLease,
  releaseGenerationLease: leaseGc.releaseGenerationLease,
  withExclusiveReaderGc: leaseGc.withExclusiveReaderGc,
  gcGenerations: leaseGc.gcGenerations,
};
