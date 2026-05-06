/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      animation: {
        'pulse-slow': 'pulse 1.5s ease-in-out infinite',
        'pulse-fast': 'pulse 0.7s ease-in-out infinite',
      },
    },
  },
  plugins: [],
};
