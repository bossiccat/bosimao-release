/**
 * 自绘内联图标（v4「暖瓷」§7.2）— lucide 不够、必须自绘的 3 枚。
 * 24 viewBox，stroke 1.75，颜色走 currentColor，由调用处用 CSS 控制色。
 * 不新增第四枚自绘，除非设计评审。
 */

type IconProps = {
  size?: number;
  className?: string;
  strokeWidth?: number;
  "aria-hidden"?: boolean | "true" | "false";
};

export function JaxPaw({ size = 24, className, strokeWidth = 1.75, "aria-hidden": ariaHidden = "true" }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden={ariaHidden}
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
    >
      <ellipse cx="6.2" cy="9.4" rx="1.9" ry="2.5" transform="rotate(-14 6.2 9.4)" />
      <ellipse cx="17.8" cy="9.4" rx="1.9" ry="2.5" transform="rotate(14 17.8 9.4)" />
      <ellipse cx="10.1" cy="5.9" rx="1.8" ry="2.4" />
      <ellipse cx="13.9" cy="5.9" rx="1.8" ry="2.4" />
      <path d="M12 11.2c3.4 0 6 2.5 6 5.1 0 2-1.5 3.2-3.1 2.6-1-.4-1.9-.6-2.9-.6s-1.9.2-2.9.6c-1.6.6-3.1-.6-3.1-2.6 0-2.6 2.6-5.1 6-5.1Z" />
    </svg>
  );
}

export function JaxWave({ size = 16, className, strokeWidth = 1.75, "aria-hidden": ariaHidden = "true" }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden={ariaHidden}
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      className={className}
    >
      <path d="M4 10v4" />
      <path d="M8 7v10" />
      <path d="M12 10v4" />
      <path d="M16 6v12" />
      <path d="M20 9v6" />
    </svg>
  );
}

export function JaxMoonNap({ size = 16, className, strokeWidth = 1.75, "aria-hidden": ariaHidden = "true" }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden={ariaHidden}
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
    >
      <path d="M15.5 4.5a7.5 7.5 0 1 0 4 9.9A8 8 0 0 1 15.5 4.5Z" />
      <path d="M17 3.5h4l-4 4h4" strokeWidth={1.5} />
    </svg>
  );
}
