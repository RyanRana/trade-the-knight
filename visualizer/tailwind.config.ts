import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0a0b10",
        panel: "#111320",
        panel2: "#161a2a",
        border: "#232842",
        muted: "#7a809a",
        text: "#e6e9f2",
        accent: "#7aa2ff",
        up: "#4ade80",
        down: "#f87171",
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
export default config;
