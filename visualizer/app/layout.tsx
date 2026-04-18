import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Knight Visualizer",
  description: "Live exchange visualizer — work & compute across assets",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">{children}</body>
    </html>
  );
}
