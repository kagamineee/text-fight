import { useEffect, useState } from 'react'
import { useGame } from '../store'

export default function Countdown({ deadline }: { deadline: number | null }) {
  const { offset } = useGame()
  const [now, setNow] = useState(Date.now())

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 200)
    return () => window.clearInterval(timer)
  }, [])

  if (!deadline) return null
  const remainMs = Math.max(0, deadline * 1000 - (now + offset))
  const seconds = Math.ceil(remainMs / 1000)
  const urgent = seconds <= 10

  return (
    <span className={`countdown ${urgent ? 'urgent' : ''}`}>
      {seconds}s
    </span>
  )
}
