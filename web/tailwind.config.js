/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: {
          50: '#f6f7f9',
          100: '#eceef2',
          200: '#d4d9e0',
          300: '#aeb6c4',
          400: '#828ea3',
          500: '#637088',
          600: '#4e5870',
          700: '#40485c',
          800: '#383e4e',
          900: '#21252f',
          950: '#151821',
        },
        accent: {
          DEFAULT: '#3b82f6',
          soft: '#eef4ff',
        },
        up: '#ef4444',   // A股红涨
        down: '#22c55e', // 绿跌
      },
      fontFamily: {
        sans: ['-apple-system', 'PingFang SC', 'Microsoft YaHei', 'Segoe UI', 'sans-serif'],
      },
    },
  },
  plugins: [],
}
