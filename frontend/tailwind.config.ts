import type { Config } from 'tailwindcss';
const config: Config = {
  content: ['./app/**/*.{js,ts,jsx,tsx,mdx}', './components/**/*.{js,ts,jsx,tsx,mdx}'],
  theme: { extend: { colors: { ink: '#0b0f12', panel: '#11171b', lime: '#c8f169', muted: '#899398' }, fontFamily: { sans: ['Inter','ui-sans-serif','system-ui'] } } },
  plugins: [],
};
export default config;
