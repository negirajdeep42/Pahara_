/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        // Operations-centre palette: near-black ground, cool slate panels.
        ink: {
          900: '#070b10',
          800: '#0b111a',
          700: '#111a25',
          600: '#16212f',
          500: '#1d2b3a',
        },
        line: '#22303f',
        muted: '#8296ab',
        // Susceptibility classes (static, 5)
        s1: '#2b83ba',
        s2: '#abdda4',
        s3: '#ffffbf',
        s4: '#fdae61',
        s5: '#d7191c',
        // Live risk classes (dynamic, 4)
        rSafe: '#22c55e',
        rWatch: '#eab308',
        rWarn: '#f97316',
        rEvac: '#dc2626',
      },
      fontFamily: {
        sans: ['Inter', 'Segoe UI', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'Consolas', 'ui-monospace', 'monospace'],
      },
      keyframes: {
        'pulse-danger': {
          '0%, 100%': { borderColor: 'rgba(220,38,38,0.35)', boxShadow: '0 0 0 0 rgba(220,38,38,0)' },
          '50%': { borderColor: 'rgba(220,38,38,1)', boxShadow: '0 0 18px 0 rgba(220,38,38,0.45)' },
        },
        'slide-in': {
          from: { transform: 'translateX(100%)', opacity: '0' },
          to: { transform: 'translateX(0)', opacity: '1' },
        },
        'toast-in': {
          from: { transform: 'translateY(-8px)', opacity: '0' },
          to: { transform: 'translateY(0)', opacity: '1' },
        },
      },
      animation: {
        'pulse-danger': 'pulse-danger 1.4s ease-in-out infinite',
        'slide-in': 'slide-in 220ms cubic-bezier(0.22, 1, 0.36, 1)',
        'toast-in': 'toast-in 180ms ease-out',
      },
    },
  },
  plugins: [],
}
