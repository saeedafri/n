'use client'

import { useState, useEffect, useRef, useCallback } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

const API = 'http://localhost:8000/api'

type Message = { role: 'user' | 'assistant'; content: string }
type Chip = { label: string; mape: number }

export default function ChatPage() {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [models, setModels] = useState<string[]>([])
  const [selectedModel, setSelectedModel] = useState('')
  const [chips, setChips] = useState<Chip[]>([])
  const [listening, setListening] = useState(false)
  const [speaking, setSpeaking] = useState(false)
  const [streamingText, setStreamingText] = useState('')

  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef  = useRef<HTMLTextAreaElement>(null)
  const recognitionRef = useRef<any>(null)

  // Load models and context on mount
  useEffect(() => {
    fetch(`${API}/models`)
      .then(r => r.json())
      .then(d => {
        setModels(d.models || [])
        setSelectedModel(d.models?.[0] || 'llama3.1:8b')
      })
      .catch(() => { setModels(['llama3.1:8b']); setSelectedModel('llama3.1:8b') })

    fetch(`${API}/context`)
      .then(r => r.json())
      .then(d => {
        if (d.context) {
          // Parse model chips from context
          const lines = d.context.split('\n')
          const parsed: Chip[] = []
          lines.forEach((l: string) => {
            const m = l.match(/^\s+(\w+):\s+MAPE=([\d.]+)%/)
            if (m) parsed.push({ label: m[1], mape: parseFloat(m[2]) })
          })
          setChips(parsed)
        }
        if (d.model) setSelectedModel(d.model)
      })
      .catch(() => {})
  }, [])

  // Auto-scroll
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingText])

  const sendMessage = useCallback(async (text: string) => {
    if (!text.trim() || loading) return
    const userMsg: Message = { role: 'user', content: text }
    setMessages(prev => [...prev, userMsg])
    setInput('')
    setLoading(true)
    setStreamingText('')

    try {
      const resp = await fetch(`${API}/chat/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: [...messages, userMsg],
          model: selectedModel,
        }),
      })

      if (!resp.body) throw new Error('No response body')
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let full = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        const lines = decoder.decode(value).split('\n')
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const data = line.slice(6)
          if (data === '[DONE]') break
          try {
            const chunk = JSON.parse(data)
            if (chunk.text) {
              full += chunk.text
              setStreamingText(full)
            }
          } catch {}
        }
      }

      setMessages(prev => [...prev, { role: 'assistant', content: full }])
      setStreamingText('')
    } catch (e) {
      setMessages(prev => [...prev, { role: 'assistant', content: 'Error connecting to backend. Is it running?' }])
    } finally {
      setLoading(false)
      setTimeout(() => inputRef.current?.focus(), 100)
    }
  }, [messages, selectedModel, loading])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage(input)
    }
  }

  // Voice input
  const toggleMic = () => {
    const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
    if (!SR) { alert('Voice input requires Chrome or Edge'); return }

    if (listening) {
      recognitionRef.current?.stop()
      setListening(false)
      return
    }

    const r = new SR()
    r.lang = 'en-US'
    r.interimResults = true
    r.maxAlternatives = 1
    recognitionRef.current = r

    r.onstart  = () => setListening(true)
    r.onend    = () => setListening(false)
    r.onerror  = () => setListening(false)

    r.onresult = (e: any) => {
      const transcript = Array.from(e.results)
        .map((res: any) => res[0].transcript)
        .join('')
      setInput(transcript)
      if (e.results[e.results.length - 1].isFinal) {
        setListening(false)
        sendMessage(transcript)
      }
    }
    r.start()
  }

  // Text to speech
  const speakLast = () => {
    if (!window.speechSynthesis) return
    if (speaking) { window.speechSynthesis.cancel(); setSpeaking(false); return }
    const last = [...messages].reverse().find(m => m.role === 'assistant')
    if (!last) return
    const clean = last.content.replace(/\*\*/g, '').replace(/\*/g, '').replace(/#+\s/g, '').replace(/`/g, '')
    const u = new SpeechSynthesisUtterance(clean)
    u.rate = 1.0; u.pitch = 1.0
    const voices = window.speechSynthesis.getVoices()
    const v = voices.find(v => v.lang === 'en-US' && v.name.includes('Google')) || voices.find(v => v.lang.startsWith('en'))
    if (v) u.voice = v
    u.onstart = () => setSpeaking(true)
    u.onend   = () => setSpeaking(false)
    u.onerror = () => setSpeaking(false)
    window.speechSynthesis.speak(u)
  }

  const chipColor = (mape: number) => {
    if (mape < 2) return { bg: '#D1FAE5', text: '#065F46', border: '#6EE7B7' }
    if (mape < 5) return { bg: '#FEF3C7', text: '#92400E', border: '#FCD34D' }
    return { bg: '#FEE2E2', text: '#991B1B', border: '#FCA5A5' }
  }

  return (
    <div className="flex flex-col h-screen max-w-3xl mx-auto px-4">

      {/* ── Header ── */}
      <div className="pt-8 pb-4 border-b border-gray-100">
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-lg font-700 text-gray-900" style={{fontWeight:700}}>AI Forecast Assistant</h1>
            <p className="text-xs text-gray-400 mt-0.5">Powered by Ollama</p>
          </div>
          <div className="flex items-center gap-2">
            {chips.length > 0 && chips.map(c => {
              const col = chipColor(c.mape)
              return (
                <span key={c.label} className="text-xs font-600 px-2.5 py-1 rounded-full"
                  style={{ background: col.bg, color: col.text, border: `1px solid ${col.border}`, fontWeight: 600 }}>
                  {c.label} · {c.mape.toFixed(2)}%
                </span>
              )
            })}
            {chips.length === 0 && (
              <span className="text-xs text-gray-300">Run Analysis to load context</span>
            )}
          </div>
        </div>
      </div>

      {/* ── Messages ── */}
      <div className="flex-1 overflow-y-auto py-6 flex flex-col gap-4">

        {/* Message history */}
        {messages.map((msg, i) => (
          <div key={i} className={`msg-in flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            {msg.role === 'user' ? (
              <div className="max-w-[72%] px-4 py-2.5 rounded-[18px_18px_4px_18px] text-sm leading-relaxed text-white"
                style={{ background: '#C8102E' }}>
                {msg.content}
              </div>
            ) : (
              <div className="max-w-[88%] text-sm leading-relaxed text-gray-800 prose">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
              </div>
            )}
          </div>
        ))}

        {/* Streaming */}
        {loading && (
          <div className="msg-in flex justify-start">
            {streamingText ? (
              <div className="max-w-[88%] text-sm leading-relaxed text-gray-800 prose">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{streamingText + '▌'}</ReactMarkdown>
              </div>
            ) : (
              <div className="flex gap-1.5 items-center px-4 py-3 bg-white rounded-2xl border border-gray-100 shadow-sm">
                <div className="typing-dot w-2 h-2 rounded-full bg-red-500"></div>
                <div className="typing-dot w-2 h-2 rounded-full bg-red-500"></div>
                <div className="typing-dot w-2 h-2 rounded-full bg-red-500"></div>
                <span className="text-xs text-gray-400 ml-1">Thinking...</span>
              </div>
            )}
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* ── Input zone — Claude style ── */}
      <div className="pb-6">
        <div className="bg-white border border-gray-200 rounded-2xl shadow-md focus-within:border-red-400 focus-within:shadow-[0_0_0_3px_rgba(200,16,46,0.08)] transition-all">

          {/* Text area */}
          <textarea
            ref={inputRef}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask anything about your forecast..."
            rows={1}
            style={{ resize: 'none', minHeight: '44px', maxHeight: '160px' }}
            className="w-full px-4 pt-3 pb-1 text-sm text-gray-900 placeholder-gray-400 bg-transparent outline-none leading-relaxed overflow-y-auto"
            onInput={e => {
              const t = e.target as HTMLTextAreaElement
              t.style.height = 'auto'
              t.style.height = Math.min(t.scrollHeight, 160) + 'px'
            }}
          />

          {/* Bottom toolbar */}
          <div className="flex items-center justify-between px-3 pb-2.5 pt-1">
            <div className="flex items-center gap-2">

              {/* Model selector */}
              <select
                value={selectedModel}
                onChange={e => setSelectedModel(e.target.value)}
                className="text-xs font-500 text-gray-600 bg-gray-50 border border-gray-200 rounded-lg px-2.5 py-1.5 outline-none cursor-pointer hover:border-red-300 transition-colors"
                style={{ fontWeight: 500 }}>
                {models.map(m => <option key={m} value={m}>{m}</option>)}
              </select>

              {/* Mic */}
              <button
                onClick={toggleMic}
                title={listening ? 'Stop listening' : 'Voice input (Chrome/Edge)'}
                className={`flex items-center justify-center w-8 h-8 rounded-lg border transition-all duration-150
                  ${listening
                    ? 'bg-red-50 border-red-400 text-red-600 mic-listening'
                    : 'bg-gray-50 border-gray-200 text-gray-500 hover:border-red-300 hover:text-red-500 hover:bg-red-50'
                  }`}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                     stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
                  <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
                  <line x1="12" y1="19" x2="12" y2="23"/>
                  <line x1="8" y1="23" x2="16" y2="23"/>
                </svg>
              </button>

              {/* Speaker */}
              <button
                onClick={speakLast}
                title={speaking ? 'Stop speaking' : 'Read last reply aloud'}
                className={`flex items-center justify-center w-8 h-8 rounded-lg border transition-all duration-150
                  ${speaking
                    ? 'bg-red-50 border-red-400 text-red-600'
                    : 'bg-gray-50 border-gray-200 text-gray-500 hover:border-red-300 hover:text-red-500 hover:bg-red-50'
                  }`}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                     stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
                  {speaking
                    ? <line x1="23" y1="9" x2="23" y2="15"/>
                    : <><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></>
                  }
                </svg>
              </button>

              {/* Clear chat */}
              {messages.length > 0 && (
                <button
                  onClick={() => setMessages([])}
                  title="Clear chat"
                  className="flex items-center justify-center w-8 h-8 rounded-lg border border-gray-200 bg-gray-50 text-gray-500 hover:border-red-300 hover:text-red-500 hover:bg-red-50 transition-all duration-150">
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
                       stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
                    <line x1="18" y1="6" x2="6" y2="18"/>
                    <line x1="6" y1="6" x2="18" y2="18"/>
                  </svg>
                </button>
              )}

            </div>

            {/* Send button */}
            <button
              onClick={() => sendMessage(input)}
              disabled={!input.trim() || loading}
              className="flex items-center justify-center w-8 h-8 rounded-lg transition-all duration-150 disabled:opacity-40"
              style={{ background: input.trim() && !loading ? '#C8102E' : '#E5E7EB' }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                   stroke={input.trim() && !loading ? 'white' : '#9CA3AF'}
                   strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <line x1="12" y1="19" x2="12" y2="5"/>
                <polyline points="5 12 12 5 19 12"/>
              </svg>
            </button>
          </div>
        </div>
        <p className="text-center text-xs text-gray-300 mt-2">
          Press Enter to send · Shift+Enter for new line · Voice works in Chrome/Edge
        </p>
      </div>
    </div>
  )
}
