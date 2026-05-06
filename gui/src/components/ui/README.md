# UI components

This directory mirrors the [shadcn/ui](https://ui.shadcn.com) pattern: every
component is **copied** into the repo (no runtime dependency on a component
library). Adding a component:

```bash
npx shadcn@latest add button
```

Phase 0 ships none — Phase 1 features pull in `button`, `card`, `tabs`,
`dialog`, `select`, `input`, `table`, `badge`, `tooltip` as needed.
