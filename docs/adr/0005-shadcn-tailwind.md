# 0005 — shadcn/ui + Tailwind for components

- **Status:** Accepted
- **Date:** 2026-05-02

## Context

We need a component story that:

- Looks polished out of the box (production software feel).
- Is theme-able for future light/dark modes.
- Doesn't lock us into a vendor's design language.
- Lets us patch components without forking dependencies.

The candidates: **MUI**, **Ant Design**, **Mantine**, **Chakra UI**, and
**shadcn/ui + Tailwind**.

## Decision

Use **shadcn/ui** components on top of **Tailwind CSS 3.4**.

shadcn/ui isn't a runtime dependency — its CLI **copies** components into
`src/components/ui/` where we own them. Styling is Tailwind utility classes
plus CSS variables for theming.

## Consequences

- ✅ Zero runtime component-library bundle weight.
- ✅ Components are ours to mod — when the design needs an extra prop or a
  variant, we edit the local file rather than wrapping or upstreaming.
- ✅ Theming via CSS variables is straightforward; dark mode is a class
  toggle on the document.
- ✅ Tailwind utility classes are more discoverable than nested CSS-in-JS,
  and they kill specificity wars.
- ⚠️ More copy-paste setup than `import { Button } from 'mui'`. We treat
  this as a one-time cost per component.
- ⚠️ Designers / contributors must learn Tailwind (low ramp).
- ⚠️ Tailwind 4 (latest) is in preview; we pin to 3.4 stable for now.

## Alternatives considered

### MUI / Ant Design / Mantine

- ✅ Batteries-included (date pickers, charts, tables).
- ❌ Hard to theme away from the library's house style.
- ❌ Significant runtime bundle (~100–300 KB even tree-shaken).
- ❌ Component customisation often requires `sx={}` overrides or CSS
  shadow-DOM hacks.
- ❌ When a component doesn't quite fit, the only escape is to fork — we'd
  rather start owning instead.

### Chakra UI

- ✅ Hooks-first API, decent themability.
- ❌ Runtime CSS-in-JS performance hit.
- ❌ Smaller community than the alternatives above.

### Plain CSS / CSS Modules

- ✅ No framework lock-in.
- ❌ Re-implementing accessibility for menus, popovers, etc. is significant
  ongoing cost.

## What this does NOT mean

- We don't pull every shadcn component eagerly. Each Phase 1 feature adds
  what it needs. Today (Phase 0) we ship none.
- Custom one-off components live next to the feature, not under
  `components/ui/`. That folder is reserved for primitives that >1 feature
  uses.

## Migration off-ramp

If shadcn/ui's pace stalls, we own all the source — switching to Radix UI
primitives directly (which shadcn wraps) or to a different Tailwind
component library is incremental, not a big-bang rewrite.
