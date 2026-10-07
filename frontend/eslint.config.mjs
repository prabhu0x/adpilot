import nextVitals from 'eslint-config-next/core-web-vitals';
import nextTs from 'eslint-config-next/typescript';

const config = [
  ...nextVitals,
  ...nextTs,
  {
    ignores: ['.next/**', 'out/**', 'build/**', 'node_modules/**', 'next-env.d.ts'],
    rules: {
      // API DTOs are dynamic during this prototype; strict TypeScript checks still run in CI/build.
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },
];

export default config;
