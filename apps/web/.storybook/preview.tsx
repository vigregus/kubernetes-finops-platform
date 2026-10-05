import type { Preview } from "@storybook/react-vite"
import "../src/index.css"

const preview: Preview = {
  parameters: {
    layout: "fullscreen",
    controls: {
      matchers: {
        color: /(background|color)$/i,
        date: /Date$/i,
      },
    },
    backgrounds: {
      options: {
        surface: { name: "Surface", value: "#fff8f6" },
        dark: { name: "Dark canvas", value: "#1c1917" },
      },
    },
    viewport: {
      options: {
        phone: { name: "Phone 390×844", styles: { width: "390px", height: "844px" }, type: "mobile" },
        phoneSmall: { name: "Small phone 360×640", styles: { width: "360px", height: "640px" }, type: "mobile" },
        tablet: { name: "Tablet 820×1180", styles: { width: "820px", height: "1180px" }, type: "tablet" },
      },
    },
    options: {
      // Порядок уровней в боковой панели: от простого к сложному, как в документации.
      storySort: { order: ["Introduction", "Foundations", "Atoms", "Molecules", "Organisms", "Templates", "Pages"] },
    },
    a11y: {
      test: "todo",
    },
  },
  initialGlobals: {
    backgrounds: { value: "surface" },
  },
}

export default preview
