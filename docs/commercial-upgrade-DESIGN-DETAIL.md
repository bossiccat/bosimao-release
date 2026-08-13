# 波斯猫双工语音商业升级：跨端设计细化

> 版本：1.0
> 设计寄存器：Product
> 唯一上游契约：`commercial-upgrade-SPEC.md` v1.1、`commercial-upgrade-DESIGN.md`
> 知识库：`token-standard.md`；AI 行业文件实际使用 `references/industries/ai-native.md`
> 三轴刻度：DESIGN_VARIANCE=4 / MOTION_INTENSITY=3 / VISUAL_DENSITY=6
> 当前商业发布裁决：FAIL。本文件是实现契约，不代表代码、SDK 或真机能力已通过。

## 1. 设计系统冻结

### 1.1 体验方向

- 关键词：冷静、可诊断、持续可用、克制、可信。
- 参考：Linear 的信息层级、Stripe 的错误解释、Android 原生后台可见性、Apple HIG 的状态连续性。
- 产品首屏直接呈现会话状态、转写、回复和可执行恢复，不制作营销 Hero。
- 深色冷调中性色占主要面积；天蓝强调色只用于当前主操作与可见焦点，每屏不超过两处。
- 禁止紫色到粉色渐变、渐变文字、装饰性毛玻璃、发光边框堆叠、弹跳或弹性缓动。

### 1.2 Token 来源与使用

Web 唯一 Token 源：

- `pet-ui/src/styles/design-tokens.json`：机器可读四层 Token。
- `pet-ui/src/styles/design-tokens.css`：CSS 变量实现。

颜色只允许在 Token 定义文件出现字面量；业务组件必须使用语义变量。四层对应：

- A1 identity：背景、表面、前景、强调色、边框、字体。
- A1 structure：字号、间距、圆角、容器、触控尺寸。
- A2 semantic：成功、警告、危险、信息、焦点、动效。
- B slot：按钮、输入框、面板、禁用态与表面别名。
- C extension：语音体验状态色，仅作辅助，状态必须同时提供图标和文案。

### 1.3 图标与动效

- 唯一图标源为 Lucide。Web 使用 `lucide-react 0.469.0`；Android 使用同语义、同描边的 VectorDrawable。
- 尺寸：行内 16px、按钮内 20px、独立图标 24px。禁止用 emoji、PNG 或混合图标库承担功能语义。
- 即时反馈 80ms，状态反馈 150ms，内容进入 200-250ms，跨屏最多 300ms；只使用标准 ease-out 类缓动。
- reduced-motion 下关闭波形位移、脉冲、旋转和连续缩放；保留静态状态图标、文案、进度数值和主操作。

## 2. VoiceUiModel 跨端唯一状态契约

双层状态保持正交：

- 会话生命周期：`IDLE / SIGNING / ENTERING / IN_ROOM / EXITING`。
- 体验状态：`idle / requesting_permission / connecting / listening / endpointing / thinking / speaking / interrupted / recovering / error`。
- `interrupted` 是体验状态内的瞬时状态，不新增第三套状态。
- Android 与 Windows 只消费单一 `VoiceUiModel`；禁止并行业务布尔拼装 UI。

### 2.1 全状态呈现矩阵

| experienceState | 主文案 | 次文案 | 主操作 | Lucide 语义 | 可访问状态名称 |
|---|---|---|---|---|---|
| `idle` | 可以开始对话 | 麦克风尚未采集 | 开始对话 | `Mic` | 语音空闲，可以开始对话 |
| `requesting_permission` | 需要麦克风权限 | 用于把你的语音发送到当前会话 | 去设置 | `ShieldCheck` | 等待麦克风权限，可前往系统设置 |
| `connecting` | 正在连接电脑 | 正在签发并建立安全语音会话 | 取消 | `LoaderCircle` 静态化兼容 | 正在连接电脑，可以取消 |
| `listening` | 正在听 | 显示实时转写，不默认保存 | 停止聆听 | `AudioLines` | 正在收音，按钮可停止聆听 |
| `endpointing` | 正在确认输入 | 已检测到本轮语音结束 | 取消 | `CircleEllipsis` | 正在确认本轮输入 |
| `thinking` | 正在处理这句话 | 保留本轮上下文，可取消 | 取消 | `BrainCircuit` | 正在处理回复，可以取消 |
| `speaking` | 正在播报 | 开口或点击即可打断 | 停止播报 | `Volume2` | 助手正在播报，可以停止 |
| `interrupted` | 已停止播报 | 正在切回聆听 | 无独立按钮 | `VolumeX` | 播报已打断，正在恢复聆听 |
| `recovering` | 正在恢复连接 | 显示第几次重试与下次重试时间 | 立即重试 | `RefreshCw` 静态化兼容 | 正在恢复连接，可立即重试 |
| `error` | 显示分类故障原因 | 显示保留内容及下一步 | 由错误类型决定 | `CircleAlert` | 会话发生可识别错误，提供恢复操作 |

### 2.2 生命周期对体验状态的约束

| sessionLifecycle | 允许的主要体验状态 | UI 约束 |
|---|---|---|
| `IDLE` | `idle/requesting_permission/error` | 不显示已连接；可从 P0 三入口发起 |
| `SIGNING` | `connecting/recovering/error` | 取消直接回 `IDLE`，不得等待退房回调 |
| `ENTERING` | `connecting/recovering/error` | 显示进房阶段；取消幂等退出并有限时回落 |
| `IN_ROOM` | `listening/endpointing/thinking/speaking/interrupted/recovering/error` | 允许全双工打断；正常远端停止不得改变播放订阅 |
| `EXITING` | `connecting/recovering/error` | 文案为“正在结束会话”，超时仍须回 `IDLE` |

## 3. P0 发起入口与 P1 边界

P0 有三个独立入口：Android 主会话页“开始对话”、悬浮球轻触、前台通知“立即对话”。三者必须调用同一串行会话命令，进入同一 `SIGNING -> ENTERING -> IN_ROOM` TRTC 路径；任何入口失败不得使另外两个失效。

- 主会话页：可见的首要 P0 入口。
- 悬浮球：用户授权显示后可独立发起，不依赖主 Activity 保持前台。
- 前台通知：可独立发起，不依赖悬浮窗权限。
- 唤醒词仅为 P1 Beta，不属于 P0 三入口，不得用“随时唤醒”描述当前商业能力。
- 显式半双工为 P1 独立入口；不得在 P0 TRTC 失败后自动降级并继续显示全双工状态。

## 4. Android 页面设计

### 4.1 主会话页

**布局**

- 顶部 56px 状态栏：电脑设备名、连接阶段、隐私状态入口；状态图标 20px，整行目标 44px 以上。
- 中部为单列会话流：用户实时转写在前，助手回复在后。每轮以内容和 1px 分隔线分组，不堆叠相同卡片。
- 空闲时显示具体说明“点击下方按钮，与这台电脑开始语音对话”，不使用欢迎口号。
- 底部固定操作区避让安全区：56px 语音主控，加一个不抢视觉的文字输入入口；主控位置不随状态变化。

**状态与主操作**

- 覆盖第 2.1 节全部体验状态。`connecting` 显示签发或进房具体阶段；`listening` 展示非语义 RMS；`speaking` 提供停止播报。
- `error` 按类别给操作：权限为“去设置/转文字输入”，网络为“重新连接”，音频占用为“重新检测”，凭证撤销为“返回设备设置”，播放异常为“重建播放”。
- `recovering` 显示有限重试序号，不使用无限加载。
- `interrupted` 只短暂更新主文案和 `VolumeX`，随后回 `listening`。

**可访问性**

- 主控名称随状态更新，例如“开始语音对话”“停止聆听”“停止播报”“重新连接语音会话”。
- 状态区域使用 polite live region；错误使用 assertive，但同一错误不重复播报。
- 所有触摸目标至少 44x44px。文字缩放至 200% 时底部操作不遮挡会话内容。
- reduced-motion 下波形替换为静态音量刻度与文本“检测到声音”。

### 4.2 悬浮球

**布局与行为**

- 视觉直径 56px，触控目标 56px；停靠屏幕边缘并避让系统手势区。
- 常态仅显示一个 Lucide 状态图标，不显示装饰光晕。轻触独立发起 P0 TRTC；拖动只改变位置；长按打开包含“隐藏悬浮球”的系统化操作层。
- 未授予悬浮窗权限时不显示假悬浮球，转到权限引导页。

**状态**

- `idle` 为 `Mic`；`connecting` 为静态 `LoaderCircle` 加边缘进度；`listening` 为 `AudioLines`；`thinking` 为 `BrainCircuit`；`speaking` 为 `Volume2`；`interrupted` 为 `VolumeX`；`recovering` 为 `RefreshCw`；`error` 为 `CircleAlert`。
- `requesting_permission` 点击进入权限引导；`endpointing` 用 `CircleEllipsis`。
- 错误时轻触打开主会话页的分类错误，不用 Toast 代替恢复路径。

**可访问性**

- contentDescription 格式：“波斯猫语音，当前正在听，轻触打开会话”。
- reduced-motion 下不做呼吸、弹性吸边或连续波形；位置移动使用系统直接跟手，无回弹动画。

### 4.3 前台通知

**布局与操作**

- 标题为“波斯猫语音服务”，正文显示具体状态和电脑设备名。
- 固定三个操作：暂停监听、立即对话、退出；每项具备 24px VectorDrawable 和系统保证的可触控目标。
- “立即对话”是独立 P0 入口，必须进入同一 TRTC coordinator，不依赖悬浮球或主页面状态。

**状态与错误**

- `idle`：正文“等待手动开始对话”；`connecting`：显示连接阶段；`listening/speaking`：显示当前音频方向；`recovering/error`：显示分类原因，点击打开对应恢复页面。
- 凭证撤销后通知立即移除对话操作并显示“该设备已撤销”，当前会话结束回 `IDLE`。

**可访问性**

- 操作名称必须是完整动词短语，不使用只有图标的通知动作。
- 不用颜色传达状态。reduced-motion 不影响系统通知功能。

### 4.4 权限引导

**布局**

- 单任务分步页，每屏最多四项：麦克风、通知、后台运行/电池优化、悬浮窗。每项含用途、当前状态和单一操作。
- 页面标题具体，例如“允许麦克风，才能开始语音对话”；不循环触发系统权限框。
- 底部始终提供“使用文字输入”。

**状态与错误**

- `requesting_permission` 是主状态；拒绝后说明影响并提供“去设置”。永久拒绝时不再显示“再次请求”。
- 麦克风被占用显示“通话或其他录音应用正在使用麦克风”，主操作“重新检测”。
- 关闭麦克风开关必须立即停止采集、清空上行队列、释放 owner，并在页面显示“语音已关闭，仅可文字输入”。

**可访问性**

- 每个权限状态同时使用 `ShieldCheck/ShieldAlert/ShieldX` 与文字。
- 系统设置往返后保持当前步骤和焦点。所有目标至少 44x44px。

### 4.5 设备与诊断设置

**布局**

- 分为“当前设备”“隐私控制”“数据权利”“运行诊断”四组，每组最多四个首屏选项，其余渐进披露。
- 设备项显示设备名、平台、凭证状态、过期时间；撤销使用 `ShieldX`，进入二次确认。
- 四类开关必须可见：第三方云端、麦克风、后台对话、桌面捕获。切换后显示当前影响，不用成功 Toast 替代行内结果。
- 转写默认不保存；开启本地加密保存后才显示“删除本地转写”“导出本地转写”。
- 诊断导出明确列出只含脱敏指标和事件，不含凭证、原始音频、截图、代码或完整敏感文本。

**状态、操作与错误**

- 撤销确认文案：“撤销后，此设备当前语音会话会立即结束，之后需要重新配对。”主操作“确认撤销设备”，次操作“保留设备”。
- 撤销成功后当前会话立即结束并回 `IDLE`；失败显示错误码和“重试撤销”。
- 开关运行时动作失败，UI 值回滚并显示具体原因。
- 诊断 Loading 显示正在检查哪一项；Empty 显示“尚未运行诊断”；Error 提供“重新检查”；Populated 展示 SDK、模型、网络、麦克风、播放、sidecar；Edge 对长路径脱敏和中间截断。

**可访问性**

- 开关名称包含能力和当前值，例如“麦克风，已开启”。撤销对话初始焦点在标题，危险按钮不自动获焦。
- 关闭对话后焦点回到触发项。所有目标至少 44x44px；错误摘要可被屏幕阅读器定位。

## 5. Windows 页面设计

### 5.1 桌面宠物

**布局与行为**

- 宠物是紧凑状态锚点，不承载长文案。默认安静，单击或 Enter/Space 打开紧凑会话面板。
- 一级、二级提醒只更新状态点和 Lucide 图标；三级、四级才显示提醒气泡。
- 宠物不使用装饰性玻璃或外发光。状态色只作为辅助。

**状态**

- 全部体验状态使用第 2.1 节图标与短文案；`error` 点击后在面板显示分类详情。
- `speaking` 时可通过点击宠物触发停止播报；视觉先进入 `interrupted`，P95 目标不超过 300ms，再回 `listening`。
- 面板关闭不取消会话；监听中请求关闭应用时明确确认。

**可访问性**

- 锚点 44x44px 以上，角色为 button，提供 `aria-expanded` 和动态名称。
- Esc 关闭气泡或面板，焦点回宠物锚点。reduced-motion 下关闭呼吸、漂浮和波纹。

### 5.2 紧凑会话面板

**布局**

- 建议宽 384px；顶部显示设备、生命周期阶段和关闭按钮，中部是最近用户转写与当前回复，底部固定主操作。
- 使用分隔线和负空间组织，不将每条消息都包为同尺寸圆角卡片。
- 转写旁明确显示“默认不保存”或“本地加密保存已开启”。

**状态与错误**

- 完整覆盖第 2.1 节。`connecting` 区分获取凭证、进入房间、结束会话；`recovering` 显示重试次数；`error` 显示错误码、影响、保留内容与下一步。
- 下行无声错误显示“收到回复但未能播放”，操作为“重建播放”，不得通过静音订阅恢复。
- sidecar 故障显示“语音组件未运行”，操作“重新启动语音组件”；模型故障不得自动伪装为半双工。

**可访问性**

- 打开时焦点进入面板标题，Tab 顺序为状态、内容、主操作、次操作、关闭。
- 文本区可选择复制；实时状态使用 polite live region。所有按钮至少 44x44px。
- reduced-motion 下思考动画改为静态 `BrainCircuit` 和耗时文本。

### 5.3 Windows 设置

**布局**

- 最大宽度 720px，左侧页内导航只在宽度足够时出现；窄窗回退单列。
- 分区：设备、语音与后台、隐私与数据、外观与可访问性。
- 设备列表字段和撤销流程与 Android 一致；设备状态不只用颜色。
- 四类开关、转写本地加密保存、删除、导出、诊断导出均显示即时影响。

**状态与错误**

- 开关生效中禁用重复操作并显示具体动作；失败回滚值。
- 删除本地转写需二次确认；成功后行内显示删除时间，不保留正文副本。
- 导出前显示范围与目标路径；诊断导出明确脱敏边界。
- Empty：没有已配对 Android 时显示“尚未配对手机”，操作“生成一次性配对码”。Error：配对码生成失败显示原因和重试。

**可访问性**

- 原生 label 与 control 绑定；描述和错误用 `aria-describedby`。
- 所有操作至少 44x44px，焦点环可见。reduced-motion 开关立即预览静态状态。

### 5.4 Windows 运行诊断

**布局**

- 最大宽度 960px，顶部为“运行全部检查”与最近检查时间；主体按 SDK、sidecar、模型、网络、麦克风、播放六组纵向排列。
- 每行包含 `CheckCircle2/CircleAlert/XCircle`、项目名、分类结果、耗时和“查看详情”。不使用彩色侧边框。
- 详情只展示脱敏标识、状态、错误、时延、帧数和队列指标。

**状态与恢复**

- Loading：显示当前检查项和已完成数量；Empty：提示“尚未运行诊断”；Error：分类失败并提供对应操作；Populated：完整结果；Edge：超长标识中间截断并可复制脱敏值。
- sidecar 未安装或哈希错误提供“修复语音组件”；网络失败提供“重新测试网络”；麦克风占用提供“重新检测”；播放失败提供“播放测试音”。
- 导出诊断前再次确认不含凭证、原始音频、截图、代码、完整敏感文本和敏感路径。

**可访问性**

- 结果表使用语义标题和列表；状态图标具备隐藏装饰属性，结果文字完整朗读。
- 运行和导出按钮至少 44x44px。reduced-motion 下进度仅更新数字，不滚动或脉冲。

## 6. 错误分类与统一恢复文案

| 错误 | 用户文案 | 主操作 | 保留内容 |
|---|---|---|---|
| `auth_failed` | 无法验证此设备，请重新配对 | 打开设备设置 | 未发送文字草稿 |
| `credential_revoked` | 此设备已被撤销，当前会话已结束 | 重新配对 | 不保留会话凭证 |
| `handshake_timeout` | 连接电脑超时 | 重新连接 | 转写草稿与上下文标识 |
| `state_conflict` | 上一个会话操作仍在处理 | 返回当前会话 | 当前聚合状态 |
| `queue_overflow` | 语音处理积压，已停止本轮 | 重新开始本轮 | 已确认的文字，不重放音频 |
| `rate_limited` | 请求过于频繁，请稍后重试 | 显示倒计时 | 当前页面状态 |
| `credential_unavailable` | 安全会话服务暂不可用 | 重试 | 未发送文字草稿 |
| `upstream_timeout` | 语音服务响应超时 | 重试本轮 | 允许的上下文，不静默重发 |
| 麦克风占用 | 其他应用正在使用麦克风 | 重新检测 | 当前会话上下文 |
| 下行无声 | 收到回复但未能播放 | 重建播放 | 回复文本和播放诊断标识 |
| sidecar 故障 | 电脑语音组件未运行 | 重新启动语音组件 | 当前会话上下文 |

错误必须在 2 秒内显示分类原因和操作。不得暴露 token、nonce、内部地址、堆栈、原始音频或完整敏感文本。

## 7. Android 资源映射说明

本节仅定义设计交接，不修改 Android 业务代码。

### 7.1 colors.xml

Android 颜色资源与 Web Token 一一对应，值以 `design-tokens.json` 为唯一来源：

| Android resource | Web Token | 用途 |
|---|---|---|
| `jax_color_bg` | `color.identity.bg` | 应用背景 |
| `jax_color_surface` | `color.identity.surface` | 卡片与面板 |
| `jax_color_surface_raised` | `color.identity.surfaceRaised` | 交互表面 |
| `jax_color_on_surface` | `color.identity.fg` | 主文本 |
| `jax_color_on_surface_muted` | `color.identity.muted` | 次文本 |
| `jax_color_accent` | `color.identity.accent` | 主操作与焦点 |
| `jax_color_border` | `color.identity.border` | 边界 |
| `jax_color_success` | `color.semantic.success` | 成功 |
| `jax_color_warning` | `color.semantic.warning` | 警告与 interrupted |
| `jax_color_error` | `color.semantic.danger` | 错误与撤销 |
| `jax_color_info` | `color.semantic.info` | 连接与恢复 |

业务布局和 Drawable 只能引用资源名，不写颜色字面量。

### 7.2 dimens.xml

| Android resource | Token | 说明 |
|---|---|---|
| `jax_space_1` 至 `jax_space_12` | `space.*` | 4px 网格间距 |
| `jax_radius_sm/md/lg/xl` | `radius.*` | 最大 16px 圆角 |
| `jax_touch_target_min` | `size.targetMin` | 最小 44dp |
| `jax_voice_control_size` | `size.voiceControl` | 固定 56dp 主控 |
| `jax_floating_orb_size` | `size.floatingOrb` | 56dp 悬浮球 |
| `jax_icon_inline/button/standalone` | `size.icon*` | 16/20/24dp |
| `jax_gutter_phone` | `layout.gutterPhone` | 手机横向边距 |

### 7.3 themes.xml

- 基于 Material 3 深色无 ActionBar 主题。
- `colorPrimary` 映射 accent，`colorOnPrimary` 映射 accent-on，`colorSurface` 与 `colorOnSurface` 映射对应 Token。
- 系统栏使用背景和表面 Token；不得在主题或控件覆盖中引入裸色。
- 默认按钮圆角使用 md，弹窗使用 xl；危险操作使用 error 语义，不以主色伪装。
- 字体正文为 Noto Sans SC；等宽数据用 JetBrains Mono。MiSans 只有授权确认后才能作为展示字体。
- 动效使用 150ms 收敛值和标准缓动；遵循系统 Animator duration scale 与移除动画偏好。

### 7.4 Lucide VectorDrawable 语义映射

| 语义 | Lucide Web | Android VectorDrawable 建议名 |
|---|---|---|
| 开始/麦克风 | `Mic` | `ic_lucide_mic_24` |
| 停止聆听 | `MicOff` | `ic_lucide_mic_off_20` |
| 音量/RMS | `AudioLines` | `ic_lucide_audio_lines_24` |
| 播放中 | `Volume2` | `ic_lucide_volume_2_24` |
| 打断/无声 | `VolumeX` | `ic_lucide_volume_x_24` |
| 思考 | `BrainCircuit` | `ic_lucide_brain_circuit_24` |
| 处理中 | `CircleEllipsis` | `ic_lucide_circle_ellipsis_24` |
| 连接中 | `LoaderCircle` | `ic_lucide_loader_circle_24` |
| 恢复 | `RefreshCw` | `ic_lucide_refresh_cw_20` |
| 权限允许 | `ShieldCheck` | `ic_lucide_shield_check_24` |
| 权限警告 | `ShieldAlert` | `ic_lucide_shield_alert_24` |
| 撤销设备 | `ShieldX` | `ic_lucide_shield_x_20` |
| 错误 | `CircleAlert` | `ic_lucide_circle_alert_24` |
| 成功 | `CheckCircle2` | `ic_lucide_check_circle_2_20` |
| 失败 | `XCircle` | `ic_lucide_x_circle_20` |
| 设置 | `Settings` | `ic_lucide_settings_24` |
| 诊断 | `Activity` | `ic_lucide_activity_24` |
| 导出 | `Download` | `ic_lucide_download_20` |
| 删除 | `Trash2` | `ic_lucide_trash_2_20` |
| 关闭 | `X` | `ic_lucide_x_20` |

VectorDrawable 必须来自同一 Lucide 路径语义并统一 stroke 语言；功能图标不得使用 PNG 或平台 emoji。装饰图标设置为不单独朗读，图标按钮必须有 contentDescription。

## 8. 组件状态与边界

所有核心组件覆盖 Default、Hover（桌面）、Focus、Active、Disabled、Loading、Error、Empty、Success，并在页面级覆盖 Loading、Empty、Error、Populated、Edge。

- 主按钮 Loading 仍保留原宽度和动作名称，避免布局抖动。
- 超长设备名两行截断，完整值通过可访问名称提供；凭证只显示状态，不显示 Secret。
- 转写长文本按轮次折叠，不阻挡固定主控；敏感文本不进入诊断。
- 网络恢复后不得静默重发草稿，必须由用户显式继续。
- 设备撤销、删除转写是破坏性操作，需二次确认且不与主操作并列为同色。

## 9. 机械验收映射

1. 静态扫描业务组件无颜色字面量，颜色仅存在于 Token 定义文件或 Android 资源定义。
2. 使用规定 Unicode 范围扫描功能性 UI 文案与资产引用，不得发现 emoji 功能图标。
3. 扫描不得发现紫粉渐变、渐变文字、装饰性毛玻璃、发光边框套路或弹性缓动。
4. Android 主页面、悬浮球、通知“立即对话”分别独立测试，均进入同一 TRTC lifecycle；任一入口失败其余入口仍可用。
5. 唤醒词和显式半双工在 UI 中标记为 P1，不参与 P0 成功或降级判断。
6. 全部体验状态在 Android 主页面与 Windows 紧凑面板有文案、图标、主操作和可访问名称。
7. Accessibility Scanner、键盘与屏幕阅读器验证所有目标不小于 44x44px、焦点可见、状态不只依赖颜色。
8. reduced-motion 下无连续波形、呼吸、脉冲、旋转或位移动画，功能与状态含义不丢失。
9. speaking 开口或点击后先进入 `interrupted`，真机停止播报 P95 不超过 300ms，再回 `listening`。
10. 设备撤销、隐私开关、转写删除/导出、诊断导出按上游机械验收 11-12 执行并保留脱敏证据。

## 10. 自检裁决

- P0 图标：只定义 Lucide SVG/同语义 VectorDrawable；未设计 emoji 或 PNG 功能图标。
- P0 色彩：无紫粉渐变；业务组件只引用 Token。
- P0 模板味：无营销 Hero、空洞欢迎文案、装饰性毛玻璃和发光边框组合。
- 交互：无弹跳或弹性缓动；全部核心目标至少 44x44px。
- 状态：双层正交状态和单一 `VoiceUiModel` 不变；`interrupted` 仍为瞬时体验状态。
- 范围：P0 三个手动入口进入同一 TRTC 路径；唤醒词与显式半双工仅为 P1。
- 发布：商业 Release 继续保持 FAIL，直到上游发布阻断项和真机证据归零。
