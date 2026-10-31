import { copyFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { defineConfig } from 'vitepress'

export default defineConfig({
  lang: 'zh-CN',
  title: 'Sakura AI 文档',
  description: 'Sakura AI 官方文档：部署、配置、PR 审查、Issue 分析、Agent、计费和 API',
  base: '/Sakura-AI/',
  lastUpdated: true,
  srcExclude: [
    'superpowers/**',
    'agent-feature-best-practices.md',
    'agent-feature-refactor-plan.md',
    'AGENT_HARNESS_2_ACCEPTANCE.md',
    'billing-2-acceptance.md',
    'webui-information-architecture.md'
  ],
  async buildEnd({ srcDir, outDir }) {
    // Keep the repository SQL as the single source for the downloadable asset.
    await copyFile(
      resolve(srcDir, 'billing-mysql-guards.sql'),
      resolve(outDir, 'billing-mysql-guards.sql')
    )
  },
  head: [
    ['meta', { name: 'theme-color', content: '#d9679c' }],
    ['meta', { property: 'og:type', content: 'website' }],
    ['meta', { property: 'og:title', content: 'Sakura AI 文档' }],
    ['meta', { property: 'og:description', content: 'Sakura AI 官方使用与开发文档' }]
  ],
  themeConfig: {
    logo: '/logo.svg',
    siteTitle: 'Sakura AI',
    nav: [
      { text: '首页', link: '/' },
      { text: '快速开始', link: '/quick-start' },
      { text: '文档目录', link: '/README' },
      { text: 'API', link: '/api-v1-reference' },
      { text: '在线体验', link: 'https://ai.firefly520.top/' }
    ],
    sidebar: [
      {
        text: '入门',
        collapsed: false,
        items: [
          { text: '文档目录', link: '/README' },
          { text: '快速开始', link: '/quick-start' },
          { text: '部署指南', link: '/DEPLOYMENT' },
          { text: '配置参考', link: '/CONFIGURATION' },
          { text: '配置中心', link: '/configuration-center' },
          { text: 'WebUI 2.0', link: '/webui-2' }
        ]
      },
      {
        text: '功能指南',
        collapsed: true,
        items: [
          { text: 'PR 功能', link: '/PR_FEATURES_GUIDE' },
          { text: '手动审查', link: '/MANUAL_REVIEW_FEATURE' },
          { text: '审查批准', link: '/APPROVAL_FEATURE_SUMMARY' },
          { text: '审查协议', link: '/PR_REVIEW_PROTOCOL' },
          { text: '模型上下文', link: '/MODEL_CONTEXT_FEATURE' },
          { text: '项目记忆（旧版参考）', link: '/SAKURA_MEMORY_GUIDE' }
        ]
      },
      {
        text: 'Agent 系统',
        collapsed: true,
        items: [
          { text: 'Harness 2.0 运行机制', link: '/AGENT_HARNESS_RUNTIME' },
          { text: '仓库规则与 Skills', link: '/AGENT_REPOSITORY_CONTEXT' },
          { text: '插件系统', link: '/AGENT_PLUGINS' },
          { text: 'Skills 实现参考', link: '/agent-skills-python-implementation' },
          { text: '文件工具实现参考', link: '/agent-file-tools-python-implementation' }
        ]
      },
      {
        text: '账户、通知与计费',
        collapsed: true,
        items: [
          { text: '统一通知中心', link: '/NOTIFICATION_CENTER' },
          { text: 'Telegram 接入', link: '/TELEGRAM_SETUP' },
          { text: '安全与 MFA', link: '/SECURITY_MFA_GUIDE' },
          { text: '配额系统', link: '/QUOTA_SYSTEM_GUIDE' },
          { text: 'Billing 2.0', link: '/billing-2' },
          { text: '旧权益迁移', link: '/billing-legacy-migration' }
        ]
      },
      {
        text: '开发与参考',
        collapsed: true,
        items: [
          { text: '技术架构', link: '/ARCHITECTURE' },
          { text: 'API v1 参考', link: '/api-v1-reference' },
          { text: '改名迁移', link: '/RENAME_MIGRATION_GUIDE' },
          { text: '贡献指南', link: 'https://github.com/Sakura520222/Sakura-AI/blob/develop/CONTRIBUTING.md' }
        ]
      }
    ],
    socialLinks: [
      { icon: 'github', link: 'https://github.com/Sakura520222/Sakura-AI' }
    ],
    search: {
      provider: 'local',
      options: {
        locales: {
          root: {
            translations: {
              button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
              modal: {
                displayDetails: '显示详情',
                resetButtonTitle: '清空搜索',
                backButtonTitle: '关闭搜索',
                noResultsText: '没有找到相关内容',
                footer: {
                  selectText: '选择',
                  selectKeyAriaLabel: '回车',
                  navigateText: '切换',
                  navigateUpKeyAriaLabel: '上',
                  navigateDownKeyAriaLabel: '下',
                  closeText: '关闭',
                  closeKeyAriaLabel: 'Esc'
                }
              }
            }
          }
        }
      }
    },
    editLink: {
      pattern: 'https://github.com/Sakura520222/Sakura-AI/edit/develop/docs/:path',
      text: '在 GitHub 上编辑本页'
    },
    lastUpdated: { text: '最后更新' },
    docFooter: { prev: '上一页', next: '下一页' },
    outline: { level: [2, 3], label: '本页目录' },
    sidebarMenuLabel: '导航',
    returnToTopLabel: '返回顶部',
    footer: {
      message: 'Sakura AI 开源项目 · 文档内容与项目版本同步维护',
      copyright: '© Sakura AI Contributors'
    }
  }
})
