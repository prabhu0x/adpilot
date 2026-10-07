# Frontend dependency audit note

**Checked:** 2026-10-08 (sandbox run)

- `npm audit --omit=dev --audit-level=moderate`: **0 production dependency vulnerabilities** with the current lockfile.
- The full audit still reports advisories in development-only Tailwind 3 / ESLint tooling. NPM's suggested remediation requires major-version changes (Tailwind 4 or an incompatible eslint-config-next downgrade), so those were not applied automatically; the current Tailwind/Next 16 configuration and build remain validated.
- Current high-severity advisory in `braces` concerns stack exhaustion from deeply nested patterns: [GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm).
- Current moderate advisory in `postcss-selector-parser` concerns quadratic parsing of flat selectors: [GHSA-rj75-hqrm-r3gf](https://github.com/advisories/GHSA-rj75-hqrm-r3gf).
- Other full-audit dependency paths include `tailwindcss`/`chokidar`/`micromatch` and `eslint-config-next`/`@next/eslint-plugin-next`/`fast-glob`; these are build/lint tooling and are not included in the production dependency audit result.

Re-run `npm audit` after dependency updates. Review major tooling upgrades and rerun `npm run lint`, `npm run typecheck`, and `npm run build` before adopting them.
