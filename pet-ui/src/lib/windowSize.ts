/**
 * 内容驱动窗口尺寸（从 App.tsx 抽出，App 保持 ≤300 行契约）。
 * 契约：常态 200×200；各面板/横幅按 UX 规格放大到不裁切的尺寸。
 */
export interface WindowSizeInputs {
  showPanel: boolean;
  showSettings: boolean;
  showCaConfirm: boolean;
  fault: boolean;
}

export function computeWindowSize(inputs: WindowSizeInputs): {
  width: number;
  height: number;
} {
  let width = 200;
  let height = 200;
  if (inputs.showPanel) {
    width = Math.max(width, 380);
    height = Math.max(height, 480);
  }
  if (inputs.showSettings) {
    width = Math.max(width, 380);
    height = Math.max(height, 560);
  }
  if (inputs.showCaConfirm) {
    width = Math.max(width, 400);
    height = Math.max(height, 580);
  }
  if (inputs.fault) {
    width = Math.max(width, 280);
    height = Math.max(height, 292);
  }
  return { width, height };
}
