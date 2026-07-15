export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Space Grotesk"', 'sans-serif'],
        mono: ['"IBM Plex Mono"', 'monospace'],
      },
      colors: {
        paper: '#F4F5F7',
        borderLight: '#E4E7EC',
        textPrimary: '#171B21',
        textSecondary: '#6B7480',
        textFaint: '#98A0AC',
        petrol: {
          DEFAULT: '#0E7C74',
          tint: '#E4F5F3',
        },
        warningAmber: {
          DEFAULT: '#B7791E',
          tint: '#FDF3E2',
        },
        errorRed: {
          DEFAULT: '#C0392B',
          tint: '#FBEAE8',
        },
      },
      boxShadow: {
        'double-soft': '0 4px 10px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04)',
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'fade-in': 'fade-in 0.15s ease-out',
        'pop-in': 'pop-in 0.18s cubic-bezier(0.16, 1, 0.3, 1)',
      },
      keyframes: {
        'fade-in': {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        'pop-in': {
          '0%': { opacity: '0', transform: 'scale(0.96) translateY(8px)' },
          '100%': { opacity: '1', transform: 'scale(1) translateY(0)' },
        },
      },
    },
  },
  plugins: [],
}