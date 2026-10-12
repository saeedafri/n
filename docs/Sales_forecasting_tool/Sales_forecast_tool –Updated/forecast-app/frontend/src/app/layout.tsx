import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: 'AI Forecast Assistant — Coresight Research',
  description: 'Sales forecast intelligence powered by Ollama',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  )
}
