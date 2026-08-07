---
kind: frontend_style
name: 前端样式系统（Tailwind CSS + React 组件库）
category: frontend_style
scope:
    - '**'
source_files:
    - apps/dsa-web/tailwind.config.js
    - apps/dsa-web/src/index.css
    - apps/dsa-web/src/App.css
    - apps/dsa-web/src/components/theme/
    - apps/dsa-web/package.json
---

该仓库的前端样式系统位于 `apps/dsa-web/` 子项目中，采用现代 React + TypeScript 技术栈，主要使用 Tailwind CSS 作为核心样式框架。以下是观察到的前端样式架构和约定：

## 1. 样式系统和工具链
- **Tailwind CSS**: 通过 `tailwind.config.js` 配置文件管理主题和设计令牌
- **Vite**: 作为构建工具，支持 CSS 模块化和资源优化
- **TypeScript**: 提供类型安全的样式定义
- **React**: 组件化 UI 开发模式

## 2. 主题系统设计
- **设计令牌**: 在 `src/components/theme/` 目录下集中管理颜色、字体等设计变量
- **多主题支持**: 支持明暗主题切换，通过 CSS 变量实现主题动态切换
- **响应式设计**: 基于 Tailwind 的断点系统实现移动端适配

## 3. 组件样式组织
- **原子化样式**: 主要使用 Tailwind 的 utility-first 方法，直接在 JSX 中应用样式类
- **组件级样式**: 复杂组件使用独立的 CSS 文件进行样式封装
- **全局样式**: 在 `src/index.css` 中定义基础样式和全局变量

## 4. 样式约定和最佳实践
- **命名规范**: 遵循 BEM 或类似命名约定的组件类名
- **颜色系统**: 使用语义化的颜色变量而非硬编码色值
- **间距系统**: 统一使用 Tailwind 的 spacing scale
- **响应式断点**: 遵循移动优先的设计原则

## 5. 第三方组件库集成
- **UI 组件**: 可能集成了 Ant Design 或其他 React UI 库
- **图标系统**: 使用统一的图标库，保持视觉一致性
- **图表库**: 集成数据可视化组件用于金融数据分析展示

## 6. 样式测试和质量保证
- **单元测试**: 包含样式相关的测试用例
- **E2E 测试**: 使用 Playwright 进行端到端样式验证
- **代码检查**: 通过 ESLint 确保样式代码质量

该前端样式系统体现了现代 Web 应用的最佳实践，通过组件化、原子化样式和主题系统实现了良好的可维护性和扩展性。