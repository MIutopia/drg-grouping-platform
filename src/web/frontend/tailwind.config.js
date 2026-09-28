/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,js}'],
  // 关键：关闭 preflight。Tailwind 的基础重置会改写 button/table 等标签样式，
  // 与 Element Plus 冲突（本项目仍用 el-table / el-dialog 等组件）。
  corePlugins: { preflight: false },
  theme: {
    extend: {
      colors: {
        warm: {
          bg: '#faf8f5',      // 暖米白底
          primary: '#4a9d9a', // 主色（青）
          gold: '#e8b86d',    // 金
          terra: '#c17767',   // 陶土红
          slate: '#6b8e8e',   // 石板灰绿
          mute: '#9ca3af',    // 中性灰
        },
      },
      fontFamily: {
        sans: ['"PingFang SC"', '"Microsoft YaHei"', 'system-ui', 'sans-serif'],
      },
      borderRadius: { '2xl': '1rem', 'xl': '0.75rem' },
    },
  },
  plugins: [],
}
