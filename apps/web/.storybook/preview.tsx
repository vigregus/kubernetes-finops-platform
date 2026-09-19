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
    a11y: {
      test: "todo",
    },
  },
  initialGlobals: {
    backgrounds: { value: "surface" },
  },
}

export default preview
