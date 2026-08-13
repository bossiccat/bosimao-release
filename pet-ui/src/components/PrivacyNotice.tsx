/**
 * 隐私说明区块（阶段 B-2）— 内容对齐 ADR-021 D1 / SPEC §9.2 / ADR-018 / ADR-020
 *
 * 说明四类开关各控制什么、数据去向、本机回环加密。是「设置 → 隐私」与 CA 确认弹窗
 * 共用的说明块，措辞逐项对齐生效点映射，不做空洞承诺。
 */
import {
  Cloud,
  FileText,
  LockKeyhole,
  MessageSquare,
  Mic,
  Monitor,
} from "lucide-react";

const EXPLAIN_ROWS = [
  {
    icon: Cloud,
    label: "云端处理",
    detail:
      "关闭后拒绝签发新的云端语音会话（fail-closed），本机不再经 TRTC 云中继发起语音。存量会话最长 600 秒内自然过期。",
  },
  {
    icon: Mic,
    label: "麦克风",
    detail:
      "控制手机端麦克风采集。关闭后停止采集、清空上行队列。当前后端仅保存状态并下发，实时停采集需手机端配合生效（后续迭代）。",
  },
  {
    icon: MessageSquare,
    label: "后台对话",
    detail:
      "控制手机锁屏/后台时的常驻监听与唤醒。关闭后后台或锁屏时暂停并退出会话。当前实时生效需手机端配合（后续迭代）。",
  },
  {
    icon: Monitor,
    label: "桌面捕获",
    detail:
      "控制本机窗口画面采集（WGC）。关闭后立即停止监控循环与会话、释放帧文件，在本机进程内即时生效。",
  },
  {
    icon: FileText,
    label: "转写持久化",
    detail:
      "默认关闭（不保存转写正文）。开启后才在本机 SQLite 以 OS 绑定密钥加密保存，可单独删除或导出，不上传第三方。",
  },
] as const;

export function PrivacyNotice() {
  return (
    <div className="privacy-notice" aria-label="隐私说明">
      <div className="pn-lock">
        <LockKeyhole size={13} strokeWidth={2} aria-hidden="true" />
        <span>本机回环加密</span>
      </div>
      <p className="pn-loop">
        本机语音与设置通信经自签名证书建立 HTTPS/WSS 加密，仅用于 127.0.0.1 本机回环，
        不用于远程连接。原始音频、截图与代码不落库。
      </p>

      <ul className="pn-list">
        {EXPLAIN_ROWS.map((row) => {
          const Icon = row.icon;
          return (
            <li key={row.label} className="pn-item">
              <span className="pn-item-head">
                <Icon size={13} strokeWidth={2} aria-hidden="true" />
                <span>{row.label}</span>
              </span>
              <span className="pn-item-detail">{row.detail}</span>
            </li>
          );
        })}
      </ul>

      <style>{`
        .privacy-notice {
          padding: 10px 12px;
          background: var(--surface-subtle);
          border: 1px solid var(--border-soft);
          border-radius: var(--radius-md);
          color: var(--fg-2);
          margin-top: 8px;
        }
        .pn-lock {
          display: flex; align-items: center; gap: 6px;
          color: var(--fg);
          font-weight: var(--weight-emphasize);
          font-size: 12px;
          margin-bottom: 6px;
        }
        .pn-loop {
          font-size: 12px;
          line-height: var(--leading-body);
          margin-bottom: 8px;
        }
        .pn-list { list-style: none; }
        .pn-item {
          display: flex; flex-direction: column; gap: 2px;
          padding: 6px 0;
          border-top: 1px solid var(--border-soft);
        }
        .pn-item:first-of-type { border-top: none; }
        .pn-item-head {
          display: flex; align-items: center; gap: 6px;
          color: var(--fg);
          font-size: 12px;
          font-weight: var(--weight-emphasize);
        }
        .pn-item-detail {
          font-size: 12px;
          line-height: var(--leading-body);
        }
      `}</style>
    </div>
  );
}
