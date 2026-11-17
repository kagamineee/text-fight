import { useGame } from './store'
import Home from './pages/Home'
import Lobby from './pages/Lobby'
import Table from './pages/Table'

export default function App() {
  const game = useGame()

  return (
    <div className="app">
      {!game.connected && (game.connecting || game.session) && (
        <div className="conn-banner">
          {game.connecting ? '正在连接服务器…' : '连接已断开，正在重连…'}
        </div>
      )}

      {!game.room && <Home />}
      {game.room && game.room.phase === 'lobby' && !game.room.over && <Lobby />}
      {game.room && (game.room.phase !== 'lobby' || game.room.over) && <Table />}

      <div className="toasts">
        {game.toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`}>
            {t.text}
          </div>
        ))}
      </div>
    </div>
  )
}
