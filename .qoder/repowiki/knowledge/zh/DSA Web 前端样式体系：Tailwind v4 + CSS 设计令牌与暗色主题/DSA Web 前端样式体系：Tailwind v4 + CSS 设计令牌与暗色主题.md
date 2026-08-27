---
kind: frontend_style
name: DSA Web 前端样式体系：Tailwind v4 + CSS 设计令牌与暗色主题
category: frontend_style
scope:
    - '**'
source_files:
    - apps/dsa-web/tailwind.config.js
    - apps/dsa-web/src/index.css
    - apps/dsa-web/src/components/theme/ThemeProvider.tsx
    - apps/dsa-web/src/components/theme/ThemeToggle.tsx
    - apps/dsa-web/index.html
    - apps/dsa-web/package.json
---

## 1. 采用的样式系统

- **框架/工具链**：React + Vite + TypeScript，样式基于 **Tailwind CSS v4**（`@tailwindcss/vite` + `tailwindcss@^4.1.18`），通过 `index.css` 中的 `@import "tailwindcss"` 和 `@config "../tailwind.config.js"` 引入。
- **主题切换**：使用 `next-themes` 的 `ThemeProvider`（默认 `dark`，启用系统主题检测，以 `class="dark"` 模式驱动），在 `src/components/theme/ThemeProvider.tsx` 中提供，并在 `apps/dsa-web/index.html` 中做首屏无闪烁的主题注入（读取 localStorage 的 `theme` key）。`ThemeToggle` 组件暴露 light / dark / system 三种模式选择。
- **原子化样式策略**：业务组件几乎全部使用 Tailwind 原子类（如 `bg-card/70`、`border-border/60`、`shadow-soft-card`、`text-cyan` 等），自定义样式集中在 `index.css` 的 `@layer base / components` 中，并通过 `tailwind.config.js` 的 `theme.extend` 扩展颜色、阴影、圆角、动画等。

## 2. 关键文件

| 文件 | 作用 |
|---|---|
| `apps/dsa-web/tailwind.config.js` | 定义 Tailwind 主题扩展：HSL 变量映射的颜色语义（primary / secondary / destructive / muted / accent / card / popover / border / input / ring）、`cyan` / `purple` / `success` / `warning` / `danger` 品牌色族、`surface-1/2/3`、`overlay-*`、`gradient-*`、`boxShadow`、`borderRadius`、`fontSize`、`spacing`、`animation` / `keyframes` |
| `apps/dsa-web/src/index.css` | 全局 CSS 入口：声明 `:root` 与 `.dark` 两套完整的设计令牌（CSS Custom Properties），包含基础色板、导航、首页、设置页、登录页、聊天气泡、回测面板、情感指标（Fear & Greed）等 token；定义 `input-surface`、`badge-*`、`glass-panel`、`gauge-*` 等通用组件样式及滚动条、动画 |
| `apps/dsa-web/src/components/theme/ThemeProvider.tsx` | 封装 `next-themes`，默认 `dark`，attribute=`class` |
| `apps/dsa-web/src/components/theme/ThemeToggle.tsx` | 主题切换下拉菜单，支持 nav / rail / default 三种变体 |
| `apps/dsa-web/index.html` | 首屏脚本读取 `localStorage.theme` 并给 `<html>` 添加 `light` / `dark` class，避免 SSR 闪烁 |
| `apps/dsa-web/package.json` | 依赖 `tailwindcss@^4.1.18`、`@tailwindcss/vite@^4.1.18`、`next-themes@^0.4.6`、`clsx`、`tailwind-merge` |

## 3. 架构与设计约定

### 3.1 设计令牌（Design Tokens）分层

- **根级 HSL 变量**：`--background` / `--foreground` / `--primary` / `--card` / `--muted` / `--accent` / `--destructive` / `--border` / `--ring` / `--radius` 等，作为 Tailwind 颜色映射源（见 `tailwind.config.js` 中 `colors.primary.DEFAULT: 'hsl(var(--primary))'` 等）。
- **派生原始值**：`--bg-subtle-raw` / `--border-dim-raw` / `--border-subtle-raw` 统一指向 `--foreground`，再派生出 `--bg-subtle`、`--border-dim`、`--border-subtle` 等半透明 token，保证明暗主题下透明度一致。
- **表面层级**：`--surface-1`（卡片）、`--surface-2`（elevated）、`--surface-3`（hover）构成 z-index 之外的视觉层级。
- **状态覆盖层**：`--overlay-hover` / `--overlay-selected` 用于选中态高亮。
- **页面专属 token 命名空间**：`--home-*`、`--settings-*`、`--login-*`、`--chat-*`、`--backtest-*`、`--nav-*`，按模块隔离，避免互相污染。
- **情感指标 token**：`--sentiment-greed` / `--sentiment-neutral` / `--sentiment-fear` 配合 `[data-sentiment=...]` 属性选择器动态着色仪表盘。

### 3.2 明暗主题实现

- 所有主题相关 token 均在 `:root`（浅色）和 `.dark`（深色）中成对声明，例如 `--primary` 在浅色为 `193 100% 43%`（青色），深色为 `190 100% 50%`。
- 情感指标 glow 强度也随主题变化：浅色 `--glow-intensity: 0.18`，深色 `--glow-intensity: 0.3`。
- 登录页在深色模式下保留“赛博风格”（`--login-bg-main: hsl(222 84% 5%)` 等），与整体暗色主题保持一致但更强调对比度。

### 3.3 组件样式组织

- 通用 UI 元素（输入框、徽章、列表项、玻璃面板、滚动条）集中在 `index.css` 的 `@layer components` 中，命名为 `.input-surface`、`.badge-*`、`.list-item`、`.glass-panel`、`.glass-panel-lg`。
- 业务组件通过 Tailwind 原子类组合这些 token，例如 `bg-card/70 shadow-soft-card backdrop-blur-md` 实现毛玻璃卡片。
- 图标来自 `lucide-react` 与 `@remixicon/react`，不内联 SVG 样式。

### 3.4 响应式策略

- 通过 Tailwind 断点（`container.center` + `screens['2xl'] = 1400px`）控制内容宽度。
- 主题切换按钮在移动端隐藏文字标签（`hidden sm:inline`），仅显示图标。
- 未看到媒体查询驱动的布局断点，主要依赖 Flex/Grid + Tailwind 响应式前缀。

## 4. 约定与约束

- **颜色必须走 HSL 变量**：新增颜色应先在 `:root` / `.dark` 中声明 CSS 变量，再通过 `tailwind.config.js` 的 `theme.extend.colors` 暴露为 Tailwind 类名，禁止在组件中硬编码十六进制色值（除少数渐变起止色外）。
- **主题开关必须经 ThemeProvider**：所有需要读/写主题的地方通过 `useTheme()` 从 `next-themes` 获取，而非直接操作 DOM class。
- **暗色模式通过 class 驱动**：`darkMode: ['class']` 配置要求 HTML 根节点存在 `class="dark"` 才生效，由 `ThemeProvider` 与 `index.html` 首屏脚本共同维护。
- **Token 命名空间隔离**：页面级样式使用 `--xxx-*` 前缀（如 `--home-*`、`--settings-*`），避免跨页面 token 冲突。
- **动画集中管理**：自定义动画（`fadeIn`、`slideUp`、`slideInRight`、`pulseGlow`、`floatIn`）在 `tailwind.config.js` 的 `animation` / `keyframes` 中声明，组件侧通过 `animate-*` 类引用。
- **Glassmorphism 统一**：面板类统一使用 `.glass-panel` / `.glass-panel-lg`，通过 `bg-card/70 backdrop-blur-md` 实现半透明磨砂效果。
- **情感指标数据驱动着色**：通过 `[data-sentiment="greed|neutral|fear"]` 等 data 属性选择器绑定到 `--sentiment-*` 变量，组件只需设置 data 属性即可自动适配主题。

## 5. 构建产物中的静态资源

- 生产构建输出位于 `static/assets/`，包含编译后的 `index-*.css` 与各页面 JS chunk（如 `LoginPage-*.js`、`BacktestPage-*.js`），由后端 `webui_frontend.py` 或 Vite 构建后托管。这些是构建产物，非源码。
