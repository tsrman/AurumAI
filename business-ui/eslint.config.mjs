import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypeScript from "eslint-config-next/typescript";

// `next lint` was removed in Next.js 16, so linting goes through the ESLint CLI.
// Since eslint-config-next 16 ships real flat configs, they are imported
// directly — the FlatCompat wrapper this file used for the eslintrc-style v15
// config now throws on them ("property 'react' closes the circle").
const config = [
  {
    ignores: [
      ".next/**",
      "out/**",
      "build/**",
      "node_modules/**",
      "next-env.d.ts",
      "playwright-report/**",
      "test-results/**",
    ],
  },
  ...nextCoreWebVitals,
  ...nextTypeScript,
  {
    rules: {
      // Dropping a property by destructuring it out into an unused binding
      // (`const { state: _, ...rest } = msg`) is a deliberate pattern here,
      // not dead code.
      "@typescript-eslint/no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", ignoreRestSiblings: true },
      ],
    },
  },
];

export default config;
