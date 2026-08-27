---
kind: frontend_style
name: 基于 Tailwind v4 + CSS 变量的双主题设计系统
category: frontend_style
scope:
    - '**'
source_files:
    - apps/dsa-web/tailwind.config.js
    - apps/dsa-web/src/index.css
    - apps/dsa-web/src/components/theme/ThemeProvider.tsx
    - apps/dsa-web/src/main.tsx
    - apps/dsa-web/src/App.css
    - apps/dsa-web/package.json
---

## 1. 技术栈与整体方案

前端位于 `apps/dsa-web`，采用 **Vite + React 19 + TypeScript** 构建。样式体系以 **Tailwind CSS v4**（`@tailwindcss/vite`）为核心，通过 `index.css` 中的 `@import "tailwindcss"` 和 `@config "../tailwind.config.js"` 引入，并声明 `darkMode: ['class']` 实现基于 class 的暗色模式切换。

主题切换由 `src/components/theme/ThemeProvider.tsx` 提供，使用 `next-themes`，默认主题为 `dark`，启用系统主题检测，并通过 `attribute="class"` 将当前主题写入根节点 class（`light` / `dark`），所有颜色变量均通过 CSS 自定义属性在 `:root` 与 `.dark` 块中分别定义。

## 2. 核心文件与职责

- `apps/dsa-web/tailwind.config.js`：集中扩展 Tailwind 主题——colors、borderColor、backgroundColor、backgroundImage、boxShadow、borderRadius、fontSize、spacing、animation、keyframes 等全部通过 `theme.extend` 注入，且大量值引用 CSS 变量（如 `hsl(var(--primary))`、`var(--surface-1)`）。
- `apps/dsa-web/src/index.css`：唯一的全局样式入口，包含约 2900 行代码，分为四大段：
  - 第 1 部分：主题变量（`:root` 与 `.dark` 两套 HSL token），覆盖基础语义色（background/foreground/card/popover/primary/secondary/muted/accent/destructive）、业务色（color-cyan/purple/success/warning/danger）、导航、Home、Settings、Login、Chat、Backtest 等模块专用 token。
  - 第 2 部分：全局 base 与组件层样式（`.input-surface`、`.badge`、`.list-item`、`.feed-item`、滚动条、动画 keyframes）。
  - 第 3 部分：金融终端风格卡片（`.terminal-card`、`.glass-panel`、`.glass-card`、`.dashboard-card`、`.gradient-border-card`），利用 `mask-composite: exclude` 实现渐变边框效果。
  - 第 4 部分：页面级组件样式（Home panel/history/rail/insight/news/strategy、Markdown prose、按钮 `.btn-primary/.btn-secondary`、聊天进度条等）。
- `apps/dsa-web/src/main.tsx`：应用入口，包裹 `<ThemeProvider>` 后渲染 App。
- `apps/dsa-web/src/App.css`：仅设置 `#root` 宽高，无样式逻辑。

## 3. 架构与设计约定

### 设计令牌（Design Tokens）分层
- **基础层**：`--background`、`--foreground`、`--primary`、`--card`、`--border`、`--radius` 等语义化 HSL token。
- **派生层**：`--bg-subtle`、`--border-dim`、`--border-subtle`、`--surface-1/2/3`、`--overlay-hover/selected` 等通过 `--bg-subtle-raw`、`--border-dim-raw` 等 raw 变量派生，确保明暗主题下透明度一致。
- **模块层**：`--home-*`、`--settings-*`、`--login-*`、`--chat-*`、`--backtest-*`、`--nav-*` 等按页面/功能域划分的专用 token。
- **Tailwind 映射层**：`tailwind.config.js` 中 `colors.*` 直接绑定到这些 CSS 变量，使组件类名（如 `bg-primary`、`text-destructive`、`border-subtle`）自动响应主题。

### 暗色模式策略
- 通过 `next-themes` 在根节点切换 `class="dark"`。
- 所有颜色变量在 `:root` 与 `.dark` 中成对定义，保持相同的变量名但不同 HSL 值。
- 暗色模式下增强 glow 强度（如 `--glow-intensity` 从 0.18 提升至 0.3）。

### 视觉风格
- 主色调为青色（cyan, `--primary` ≈ hsl(193 100% 43%/50%)），辅以紫色（accent）作为强调色，成功/警告/危险分别对应绿色、琥珀色、红色。
- 大量使用毛玻璃（`backdrop-blur-md`/`backdrop-blur-18px`）+ 半透明背景（`bg-card/70`、`hsl(var(--card) / 0.72)`）+ 渐变边框（`mask-composite: exclude`）营造“金融终端”质感。
- 阴影体系：`--shadow-soft-card`、`--shadow-soft-card-strong` 及 `glow-cyan/glow-purple/glow-success/glow-danger` 四类 box-shadow。
- 圆角统一通过 `--radius` 控制，并提供 `lg/md/sm/xl/2xl/3xl` 多档。

### 组件库与原子类使用
- 组件内部几乎全部使用 Tailwind 原子类组合（如 `flex items-center justify-between px-4 py-3 text-left transition-colors hover:bg-hover`），而非 BEM 或 CSS Modules。
- 通用 UI 元素（按钮、输入框、徽章、卡片）通过 `index.css` 中的 `.btn-primary`、`.input-surface`、`.badge-*`、`.glass-panel`、`.terminal-card` 等类复用。
- 图标来自 `lucide-react` 与 `@remixicon/react`。

### 动画与交互
- 自定义动画：`fadeIn`、`slideUp`、`slideInRight`、`floatIn`、`pulseGlow`、`spin`，在 `tailwind.config.js` 的 `animation`/`keyframes` 中注册，并在 `index.css` 中以 `.animate-*` 类暴露。
- 状态过渡：hover/focus/disabled 通过 CSS 变量驱动的 `transition` 实现平滑变化。

## 4. 约束与规范

- **主题变量必须成对定义**：新增颜色需在 `:root` 与 `.dark` 中同时声明相同变量名，否则暗色模式会回退到浅色值。
- **颜色必须通过 CSS 变量引用**：`tailwind.config.js` 中禁止硬编码颜色值，应使用 `hsl(var(--xxx))` 形式，以保证主题切换生效。
- **暗色模式开关**：通过 `next-themes` 的 `NextThemesProvider` 管理，新增页面不应自行维护 `isDark` 状态。
- **样式组织**：全局样式集中在 `index.css`，按注释区块（Base/Components/Terminal Cards/Page Utilities）划分；组件内不写独立 CSS 文件。
- **Tailwind v4 迁移说明**：文件顶部注释明确“Tailwind v4 strategy: keep theme extensions in tailwind.config.js via @config until a dedicated @theme/@utility migration”，表明当前仍沿用 v3 风格的 config 扩展方式。
- **容器断点**：`container.center: true` 且 `screens['2xl']: '1400px'`，布局以 1400px 为最大宽度居中。
- **字体**：统一使用 `Inter` → `SF Pro Display` → `Segoe UI` → system-ui 的字体栈。

## 5. 关键路径清单

- `apps/dsa-web/package.json`（依赖：tailwindcss 4.x、next-themes、react 19、vite 7.x、motion、recharts、zustand、clsx、tailwind-merge）
- `apps/dsa-web/tailwind.config.js`（主题扩展、颜色映射、动画、阴影、渐变）
- `apps/dsa-web/src/index.css`（全部 CSS 变量、全局样式、组件样式、页面样式）
- `apps/dsa-web/src/components/theme/ThemeProvider.tsx`（主题上下文）
- `apps/dsa-web/src/main.tsx`（应用入口，挂载 ThemeProvider）
- `apps/dsa-web/src/App.css`（仅 root 尺寸）
- `apps/dsa-web/tests/login-theme-tokens.test.ts`、`tests/ui_governance.test.ts`（主题与 UI 治理测试）
- `static/assets/`（构建产物，含编译后的 CSS/JS 资源）