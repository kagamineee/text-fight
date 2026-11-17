import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { connectWs } from './socket'
import './styles.css'

ReactDOM.createRoot(document.getElementById('root')!).render(<App />)

// 带着上次的会话令牌启动时，先尝试恢复座位（刷新页面不丢局）
if (localStorage.getItem('yap.token')) {
  connectWs(localStorage.getItem('yap.name') || '玩家')
}
